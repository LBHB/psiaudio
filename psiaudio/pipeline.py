import logging
log = logging.getLogger(__name__)

from collections import deque
import numpy as np


def coroutine(func):
    '''Decorator to auto-start a coroutine.'''
    def start(*args, **kwargs):
        cr = func(*args, **kwargs)
        next(cr)
        return cr
    return start


@coroutine
def capture_epoch(epoch_t0, epoch_samples, info, callback):
    '''
    Coroutine to facilitate epoch acquisition
    '''
    # This coroutine will continue until it acquires all the samples it needs.
    # It then provides the samples to the callback function and exits the while
    # loop.
    accumulated_data = []

    while True:
        tlb, data = (yield)
        samples = data.shape[-1]

        if epoch_t0 < tlb:
            # We have missed the start of the epoch. Notify the callback of this
            m = 'Missed samples for epoch of %d samples starting at %d'
            log.warning(m, epoch_samples, epoch_t0)
            callback({'signal': None, 'info': info})
            break

        elif epoch_t0 <= (tlb + samples):
            # The start of the epoch is somewhere inside `data`. Find the start
            # `i` and determine how many samples `d` to extract from `data`.
            # It's possible that data does not contain the entire epoch. In
            # that case, we just pull out what we can and save it in
            # `accumulated_data`. We then update start to point to the last
            # acquired sample `i+d` and update duration to be the number of
            # samples we still need to capture.
            i = int(round(epoch_t0 - tlb))
            d = int(round(min(epoch_samples, samples - i)))
            accumulated_data.append(data[..., i:i + d])
            epoch_t0 += d
            epoch_samples -= d

            # Check to see if we've finished acquiring the entire epoch. If so,
            # send it to the callback.
            if epoch_samples == 0:
                accumulated_data = np.concatenate(accumulated_data, axis=-1)
                callback({'signal': accumulated_data, 'info': info})
                break


@coroutine
def extract_epochs(fs, queue, epoch_size, poststim_time, buffer_size, target,
                   empty_queue_cb=None, removed_queue=None):
    # The variable `tlb` tracks the number of samples that have been acquired
    # and reflects the lower bound of `data`. For example, if we have acquired
    # 300,000 samples, then the next chunk of data received from (yield) will
    # start at sample 300,000 (remember that Python is zero-based indexing, so
    # the first sample has an index of 0).
    tlb = 0
    epoch_coroutines = {}
    prior_samples = []

    # How much historical data to keep (for retroactively capturing epochs)
    buffer_samples = round(buffer_size * fs)

    # Since we may capture very short, rapidly occurring epochs (at, say,
    # 80 per second), I find it best to accumulate as many epochs as possible before
    # calling the next target. This list will maintain the accumulated set.
    epochs = []

    # This is used for communicating events
    if removed_queue is None:
        removed_queue = deque()

    while True:
        # Wait for new data to become available
        data = (yield)
        prior_samples.append((tlb, data))

        # First, check to see what needs to be removed from
        # epoch_coroutines. If it doesn't exist, it may already have been
        # captured.
        skip = []
        n_remove = 0
        n_pop = 0
        while removed_queue:
            info = removed_queue.popleft()
            md = info['t0'], info['key']
            if md not in epoch_coroutines:
                n_remove += 1
                skip.append(md)
            else:
                epoch_coroutines.pop(md)
                n_pop += 1
        log.debug('Marked %d epochs for removal, removed %d epochs', n_remove, n_pop)

        # Send the data to each coroutine. If a StopIteration occurs,
        # this means that the epoch has successfully been acquired and has
        # been sent to the callback and we can remove it. Need to operate on
        # a copy of list since it's bad form to modify a list in-place.
        for key, epoch_coroutine in list(epoch_coroutines.items()):
            try:
                epoch_coroutine.send((tlb, data))
            except StopIteration:
                epoch_coroutines.pop(key)

        # Check to see if more epochs have been requested. Information will be
        # provided in seconds, but we need to convert this to number of
        # samples.
        n_queued = 0
        n_invalid = 0
        while queue:
            info = queue.popleft()
            key = info['t0'], info['key']
            if key in skip:
                skip.remove(key)
                n_invalid += 1
                continue
            n_queued += 1

            # Figure out how many samples to capture for that epoch
            t0 = round(info['t0'] * fs)
            info['poststim_time'] = poststim_time
            info['epoch_size'] = epoch_size if epoch_size else info['duration']
            total_epoch_size = info['epoch_size'] + poststim_time
            epoch_samples = round(total_epoch_size * fs)
            epoch_coroutine = capture_epoch(t0, epoch_samples, info,
                                            epochs.append)

            try:
                # Go through the data we've been caching to facilitate
                # historical acquisition of data. If this completes without a
                # StopIteration, then we have not finished capturing the full
                # epoch.
                for prior_sample in prior_samples:
                    epoch_coroutine.send(prior_sample)
                if key in epoch_coroutines:
                    raise ValueError('Duplicate epochs not supported')
                epoch_coroutines[key] = epoch_coroutine
            except StopIteration:
                pass

        log.debug('Queued %d epochs, %d were invalid', n_queued, n_invalid)

        tlb = tlb + data.shape[-1]

        # Once the new segment of data has been processed, pass all complete
        # epochs along to the next target.
        if len(epochs) != 0:
            target(epochs[:])
            epochs[:] = []

        # Check to see if any of the cached samples are older than the
        # specified buffer_samples and discard them.
        while True:
            oldest_samples = prior_samples[0]
            tub = oldest_samples[0] + oldest_samples[1].shape[-1]
            if tub < (tlb - buffer_samples):
                prior_samples.pop(0)
            else:
                break

        if not (queue or epoch_coroutines) and empty_queue_cb:
            # If queue and epoch coroutines are complete, call queue callback.
            empty_queue_cb()
            empty_queue_cb = None