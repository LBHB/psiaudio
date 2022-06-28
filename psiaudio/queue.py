import logging
log = logging.getLogger(__name__)

from collections import Counter
import copy
import itertools
import uuid

import numpy as np


class QueueEmptyError(Exception):
    pass


def as_iterator(x):
    if x is None:
        x = 0
    try:
        x = iter(x)
    except TypeError:
        x = itertools.cycle([x])
    return x


class AbstractSignalQueue:

    def __init__(self, fs=None):
        '''
        Parameters
        ----------
        fs : float
            Sampling rate of output that will be using this queue
        '''
        # Used internally to track intertrial silent period.
        self._delay_samples = 0

        # Dictionary of generators or arrays. Each token added to the queue has
        # a unique ID. The token is associated with either a class-based
        # generator (which can be restarted to generate a new waveform) or an
        # already-generated waveform.
        self._data = {}

        # Tracks order of items added to queue. Subclasses will incorporate
        # this into their algorithms to determine the actual ordering of the
        # stimuli (e.g., first-in, first-out, interleaved, etc.).
        self._ordering = []

        # Current waveform generator for trials.
        self._source = None

        # Total samples generated by queue
        self._samples = 0

        # Callbacks to trigger for the specified event.
        self._notifiers = {
            'added': [],
            'removed': [],
            'decrement': [],
        }

        # Is stimulus generation paused?
        self._paused = False

        # Is queue complete?
        self._empty = False

        # Sampling rate needed to generate waveforms at. This is required since
        # it's used in some calculations of timing.
        self._fs = fs

        # Start time of queue relative to acquisition start.
        self._t0 = 0

        # Tracks waveforms generated by queue. This is used in the event we
        # need to pause stimulus generation.
        self._generated = []

    @property
    def fs(self):
        return self._fs

    def get_ts(self):
        return self._samples / self._fs

    def remaining_trials(self, key):
        return self._data[key]['trials']

    def rewind_samples(self, t):
        # Reset the samples
        log.debug('Current queue time is %.3f. Attempting to rewind queue to %.3f.', self.get_ts(), t)
        t_samples = round(t * self._fs)
        t0_samples = round(self._t0 * self._fs)
        new_sample = t_samples - t0_samples
        log.debug('Current queue sample is %d. New queue sample is %d.', self._samples, new_sample)
        if new_sample > self._samples:
            raise ValueError(f'Cannot rewind past last sample generated. Requested {t:.3f}s, last sample was {self.get_ts():.3f}s.')
        self._samples = t_samples - t0_samples
        log.debug('Rewound queue samples back to %d', self._samples)
        log.debug('Absolute sample %d, relative sample %d', t_samples, t0_samples)

    def pause(self, t=None):
        log.debug('Pausing queue')
        self._paused = True
        if t is not None:
            self.cancel(t)
            self.requeue(t)
            self.rewind_samples(t)

    def cancel(self, t, delay=0):
        for info in self._generated[::-1]:
            if (info['t0'] + info['duration']) > t:
                self._notify('removed', info)

        if self._source is not None:
            info = self._generated[-1]
            if info['decrement']:
                self._data[info['key']]['trials'] += 1
            self._source = None

        self._delay_samples = int(round(delay * self._fs))

    def requeue(self, t):
        '''
        Requeues all trials scheduled after t0

        Note that this only requeues trials for which the trial counter was
        automatically decremented when the waveform was generated by the queue.
        When using artifact reject, trial counters are sometimes decremented by
        the artifact reject algorithm instead (e.g., ABRs) and the assumption
        is that the external algorithm will not recieve "cancelled" trials and,
        therefore, not decrement the counter.
        '''
        to_requeue = []
        for info in self._generated[::-1]:
            if (info['t0'] + info['duration']) <= t:
                continue
            if info['decrement']:
                to_requeue.append(info['key'])

        # to_requeue is from last to first in time. Therefore, if we
        # encounter a key that isn't present in _ordering, we should insert
        # it at the beginning of the list.
        for key in to_requeue:
            if key not in self._ordering:
                self._ordering.insert(0, key)

        log.debug('Need to requeue:: %r', dict(Counter(to_requeue)))
        trials = {k: self._data[k]['trials'] for k in self._data.keys()}
        log.debug('Current trials:: %r', trials)
        for key, count in Counter(to_requeue).items():
            log.debug('Adding %d trials for key %s back to queue', count, key)
            self._data[key]['trials'] += count
        trials = {k: self._data[k]['trials'] for k in self._data.keys()}
        log.debug('Current trials:: %r', trials)

    def resume(self, t=None):
        """
        Resumes generating trials from queue

        Parameters
        ----------
        t : float
            Time, in sec, to resume generating trials from queue.
        """
        log.debug('Resuming queue. Current timestamp is %.3f.', self.get_ts())
        if t is not None:
            self.rewind_samples(t)
        self._paused = False

    def is_empty(self):
        return self._empty

    def set_fs(self, fs):
        # Sampling rate at which samples will be generated.
        self._fs = fs

    def set_t0(self, t0):
        # Sample at which queue was started relative to experiment acquisition
        # start.
        self._t0 = t0

    def _add_source(self, source, trials, delays, duration, metadata):
        key = uuid.uuid4()
        if duration is None:
            if isinstance(source, np.ndarray):
                duration = source.shape[-1]/self._fs
            else:
                duration = source.get_duration()

        data = {
            'source': copy.deepcopy(source),
            'trials': trials,
            'requested_trials': trials,
            'delays': as_iterator(delays),
            'duration': duration,
            'metadata': metadata,
        }
        self._data[key] = data
        return key

    def get_max_duration(self):
        def get_duration(source):
            try:
                return source.get_duration()
            except AttributeError:
                return source.shape[-1]/self._fs
        return max(get_duration(d['source']) for d in self._data.values())

    def connect(self, callback, event='added'):
        if event not in self._notifiers:
            raise KeyError(f'Event "{event}" not valid')
        self._notifiers[event].append(callback)

    def _notify(self, event, info):
        for notifier in self._notifiers[event]:
            notifier(info)

    def insert(self, source, trials, delays=None, duration=None, metadata=None):
        k = self._add_source(source, trials, delays, duration, metadata)
        self._ordering.insert(k)
        return k

    def append(self, source, trials, delays=None, duration=None, metadata=None):
        k = self._add_source(source, trials, delays, duration, metadata)
        self._ordering.append(k)
        return k

    def extend(self, sources, trials, delays=None, duration=None,
               metadata=None):

        base_err = '{param} must be a scalar or a sequence of length {n}'
        n = len(sources)

        if np.iterable(trials):
            if len(trials) != n:
                raise ValueError(base_err.format('trials', n))
        else:
            trials = itertools.cycle([trials])

        if np.iterable(delays):
            if len(delays) != n:
                raise ValueError(base_err.format('delays', n))
        else:
            delays = itertools.cycle([delays])

        if np.iterable(duration):
            if len(duration) != n:
                raise ValueError(base_err.format('duration', n))
        else:
            duration = itertools.cycle([duration])

        if np.iterable(metadata):
            if len(metadata) != n:
                raise ValueError(base_err.format('metadata', n))
        else:
            metadata = itertools.cycle([metadata])

        uuids = []
        for args in zip(sources, trials, delays, duration, metadata):
            uuids.append(self.append(*args))
        return uuids

    def count_factories(self):
        return len(self._ordering)

    def count_trials(self):
        '''
        Count remaining trials
        '''
        return int(sum(v['trials'] for v in self._data.values()))

    def count_requested_trials(self):
        '''
        Count total trials
        '''
        return int(sum(v['requested_trials'] for v in self._data.values()))

    def next_key(self):
        raise NotImplementedError

    def pop_next(self, decrement=True):
        key = self.next_key()
        return key, self.pop_key(key, decrement=decrement)

    def pop_key(self, key, decrement=True):
        '''
        Removes one trial of specified key from queue and returns waveform
        '''
        data = self._data[key]
        if decrement:
            self.decrement_key(key)
        return data

    def remove_key(self, key):
        '''
        Removes key from queue entirely, regardless of number of trials
        '''
        self._ordering.remove(key)

    def decrement_key(self, key, n=1):
        """
        Decrement trials for key

        Parameters
        ----------
        key : UUID
            Key to decrement
        n : int
            Number of trials to decrement by

        Returns
        -------
        complete : bool
            True if no trials left for key, False otherwise.
        """
        if key not in self._ordering:
            raise KeyError('{} not in queue'.format(key))
        self._data[key]['trials'] -= n
        if self._data[key]['trials'] <= 0:
            self.remove_key(key)
            return True
        self._notify('decrement', {'key': key})
        return False

    def _get_samples_waveform(self, samples):
        if samples > len(self._source):
            waveform = self._source
            self._source = None
        else:
            waveform = self._source[:samples]
            self._source = self._source[samples:]
        return waveform

    def _get_samples_generator(self, samples):
        samples = min(self._source.n_samples_remaining(), samples)
        waveform = self._source.next(samples)
        if self._source.is_complete():
            self._source = None
        return waveform

    def next_trial(self, decrement=True):
        '''
        Setup the next trial

        This has immediate effect. If you call this (from external code), the
        current trial will not finish.
        '''
        key, data = self.pop_next(decrement=decrement)

        # Source can either be a generator or an array. If generator, be sure to reset.
        self._source = data['source']
        try:
            self._source.reset()
            self._get_samples = self._get_samples_generator
        except AttributeError:
            self._source = data['source']
            self._get_samples = self._get_samples_waveform

        # Now, determine the next ITI (as specified by the delay generator)
        delay = next(data['delays'])
        self._delay_samples = int(round(delay*self._fs))
        if self._delay_samples < 0:
            raise ValueError('Invalid option for delay samples')

        t0 = self._t0 + (self._samples/self._fs)
        info = {
            't0': t0,                       # Time re. acq. start
            'duration': data['duration'],   # Duration of token
            'key': key,                     # Unique ID
            'metadata': data['metadata'],   # Metadata re. token
            'decrement': decrement,         # Automatically decr. trial ctr.?
        }
        self._generated.append(info)
        self._notify('added', info)

    def pop_buffer(self, samples, decrement=True):
        '''
        Return the requested number of samples

        Removes stack of waveforms in order determined by `pop`, but only
        returns requested number of samples.  If a partial fragment of a
        waveform is returned, the remaining part will be returned on subsequent
        calls to this function.
        '''
        waveforms = []
        while samples > 0:
            try:
                waveform = self._pop_buffer(samples, decrement)
            except QueueEmptyError:
                log.info('Queue is empty')
                waveform = np.zeros(samples)
                self._empty = True
            samples -= len(waveform)
            self._samples += len(waveform)
            waveforms.append(waveform)
        waveform = np.concatenate(waveforms, axis=-1)
        log.trace('Generated %d samples', len(waveform))
        return waveform

    def _pop_buffer(self, samples, decrement):
        '''
        Encodes logic for deciding what segment needs to be generated. It must
        return *up to* the number of samples requested, but can be less if
        needed.
        '''
        # If paused, return a stream of zeros.
        if self._paused:
            return np.zeros(samples)

        # Load samples from current source
        if self._source is not None:
            return self._get_samples(samples)

        # Insert intertrial interval delay if one exists
        if self._delay_samples > 0:
            n = min(self._delay_samples, samples)
            self._delay_samples -= n
            return np.zeros(n)

        # Set up next trial
        if self._source is None:
            self.next_trial(decrement)
            return np.empty(0)

    def get_closest_key(self, t):
        for info in self._generated[::-1]:
            if info['t0'] <= t:
                return info['key']
        return None

    def get_info(self, key):
        return self._data[key].copy()


class FIFOSignalQueue(AbstractSignalQueue):
    '''
    Return waveforms based on the order they were added to the queue
    '''

    def next_key(self):
        if len(self._ordering) == 0:
            raise QueueEmptyError
        return self._ordering[0]


class InterleavedFIFOSignalQueue(AbstractSignalQueue):
    '''
    Return waveforms based on the order they were added to the queue; however,
    trials are interleaved.
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._i = -1
        self._complete = False

    def next_key(self):
        if self._complete:
            raise QueueEmptyError
        self._i = (self._i + 1) % len(self._ordering)
        return self._ordering[self._i]

    def decrement_key(self, key, n=1):
        if key not in self._ordering:
            raise KeyError('{} not in queue'.format(key))
        self._data[key]['trials'] -= n
        for key, data in self._data.items():
            if data['trials'] > 0:
                return False
        self._complete = True
        return True

    def count_trials(self):
        return sum(max(v['trials'], 0) for v in self._data.values())


class RandomSignalQueue(AbstractSignalQueue):
    '''
    Return waveforms in random order
    '''

    def next_key(self):
        if len(self._ordering) == 0:
            raise QueueEmptyError
        i = np.random.randint(0, len(self._ordering))
        return self._ordering[i]


class BlockedRandomSignalQueue(InterleavedFIFOSignalQueue):

    def __init__(self, fs, seed=0, *args, **kwargs):
        super().__init__(fs, *args, **kwargs)
        self._i = []
        self._rng = np.random.RandomState(seed)

    def next_key(self):
        if self._complete:
            raise QueueEmptyError
        if not self._i:
            # The blocked order is empty. Create a new set of random indices.
            i = np.arange(len(self._ordering))
            self._rng.shuffle(i)
            self._i = i.tolist()
        i = self._i.pop()
        return self._ordering[i]


class GroupedFIFOSignalQueue(FIFOSignalQueue):
    '''
    Like the FIFOSignalQueue, this queue iterates through each waveform in the
    order it was added. However, the iteration is performed in blocks. If the
    block size is 4 and you have 8 waveforms queued:

        A B C D E F G H

    The queue iterates through A B C D until all trials have been presented,
    then it shifts to E F G H.
    '''

    def __init__(self, group_size, fs=None, *args, **kwargs):
        super().__init__(fs, *args, **kwargs)
        self._group_size = group_size
        self._i = -1

    def next_key(self):
        if len(self._ordering) == 0:
            raise QueueEmptyError
        self._i = (self._i + 1) % self._group_size
        return self._ordering[self._i]

    def decrement_key(self, key, n=1):
        if key not in self._ordering:
            raise KeyError('{} not in queue'.format(key))
        self._data[key]['trials'] -= n

        # Check to see if the group is complete. Return from method if not
        # complete.
        for key in self._ordering[:self._group_size]:
            if self._data[key]['trials'] > 0:
                return False

        # If complete, remove the keys
        for key in self._ordering[:self._group_size]:
            self.remove_key(key)

        return True


class BlockedFIFOSignalQueue(GroupedFIFOSignalQueue):
    '''
    Like the GroupedFIFOSignalQueue except block size is automatically set to
    the number of waveforms queued. If you have 8 waveforms queued:

        A B C D E F G H

    The queue iterates through A B C D E F G H until all trials have been
    presented.
    '''
    def __init__(self, fs=None, *args, **kwargs):
        super().__init__(group_size=0, fs=fs, *args, **kwargs)

    def append(self, *args, **kwargs):
        self._group_size += 1
        return super().append(*args, **kwargs)


queues = {
    'first-in, first-out': FIFOSignalQueue,
    'interleaved first-in, first-out': InterleavedFIFOSignalQueue,
    'blocked first-in, first-out': BlockedFIFOSignalQueue,
    'grouped first-in, first-out': GroupedFIFOSignalQueue,
    'random': RandomSignalQueue,
    'blocked random': BlockedRandomSignalQueue,
}
