import pytest

import numpy as np
from numpy.testing import assert_array_almost_equal, assert_array_equal
from scipy import signal

from psiaudio import calibration, stim, util

from conftest import assert_chunked_generation


def test_tone_factory():
    fs = 100e3
    frequency = 5e3
    # This is in dB!
    level = 0
    phase = 0
    polarity = 1
    cal = calibration.FlatCalibration.unity()

    tone = stim.ToneFactory(
        fs=100e3,
        frequency=frequency,
        level=level,
        phase=phase,
        polarity=polarity,
        calibration=cal
    )
    samples = 1000

    # Check initial segment
    waveform = tone.next(samples)
    t = np.arange(0, samples, dtype=np.double) / fs
    expected = np.sqrt(2) * np.cos(2 * np.pi * t * frequency)
    assert_array_equal(waveform, expected)

    # Check next segment
    t = np.arange(samples, samples*2, dtype=np.double) / fs
    waveform = tone.next(samples)
    expected = np.sqrt(2) * np.cos(2 * np.pi * t * frequency)
    assert_array_equal(waveform, expected)

    # Verify reset
    tone.reset()
    samples = 2000
    t = np.arange(0, samples, dtype=np.double) / fs
    waveform = tone.next(samples)
    expected = np.sqrt(2) * np.cos(2 * np.pi * t * frequency)
    assert_array_equal(waveform, expected)


def test_silence_factory(silence_fill_value):
    silence = stim.SilenceFactory(fill_value=silence_fill_value)
    waveform = silence.next(100)
    expected = np.full(100, silence_fill_value)
    assert_array_equal(waveform, expected)

    silence.reset()
    waveform = silence.next(100)
    assert_array_equal(waveform, expected)


def test_cos2envelope_shape():
    fs = 100e3
    offset = 0
    samples = 400000
    start_time = 0
    rise_time = 1.0
    duration = 4.0

    expected = np.ones(400000)
    t_samples = round(rise_time*fs)
    t_env = np.linspace(0, rise_time, t_samples, endpoint=False)
    ramp = np.sin(2*np.pi*t_env*1.0/rise_time*0.25)**2
    expected[:t_samples] = ramp
    ramp = np.sin(2*np.pi*t_env*1.0/rise_time*0.25 + np.pi/2)**2
    expected[-t_samples:] = ramp

    actual = stim.cos2envelope(fs, duration, rise_time, start_time, offset,
                               samples)
    assert_array_almost_equal(actual, expected, 4)


def test_cos2envelope_factory():
    fs = 100e3
    frequency = 5e3
    # This is in dB!
    level = 0
    phase = 0
    polarity = 1
    cal = calibration.FlatCalibration.unity()

    tone = stim.ToneFactory(
        fs=fs,
        frequency=frequency,
        level=level,
        phase=phase,
        polarity=polarity,
        calibration=cal
    )

    duration = 1
    rise_time = 0.5e-3

    ramped_tone = stim.Cos2EnvelopeFactory(
        fs=fs,
        duration=duration,
        rise_time=rise_time,
        input_factory=tone,
    )

    w1 = ramped_tone.get_samples_remaining()

    # Samples don't actually return to zero at the boundaries based on how we
    # do the calculations.
    assert w1[0] == pytest.approx(0, abs=1e-2)
    assert w1[-1] == pytest.approx(0, abs=1e-2)
    assert w1.shape == (int(fs),)
    assert_array_almost_equal(w1[1:1001], w1[-1000:][::-1])

    w2 = stim.ramped_tone(fs=fs, frequency=frequency, level=level,
                          calibration=cal, duration=duration,
                          rise_time=rise_time)

    assert_array_equal(w1, w2)


def test_cos2envelope_partial_generation(fs):
    duration = 10e-3
    tone_duration = 5e-3
    rise_time = 0.5e-3
    samples = round(duration*fs)

    # Make sure that the envelope is identical even if we delay the start
    y0 = stim.cos2envelope(fs, duration=tone_duration, rise_time=rise_time,
                           samples=samples)
    for start_time in (0.1e-3, 0.2e-3, 0.3e-3):
        y1 = stim.cos2envelope(fs, offset=0, samples=samples,
                               start_time=start_time, rise_time=rise_time,
                               duration=tone_duration)
        n = round(start_time * fs)
        np.testing.assert_allclose(y0[:-n], y1[n:])

    # Now, test piecemeal generation
    partition_size = 0.1e-3
    partition_samples = round(partition_size * fs)
    n_partitions = round(samples / partition_samples)
    for start_time in (0, 0.1e-3, 0.2e-3, 0.3e-3):
        env = stim.Cos2EnvelopeFactory(fs, rise_time=rise_time,
                                       duration=tone_duration,
                                       input_factory=stim.SilenceFactory(fill_value=1),
                                       start_time=start_time)
        y1 = [env.next(partition_samples) for i in range(n_partitions)]
        y1 = np.concatenate(y1)
        n = round(start_time * fs)
        if n > 0:
            np.testing.assert_allclose(y0[:-n], y1[n:])
        else:
            np.testing.assert_allclose(y0, y1)


@pytest.fixture(scope='module', params=['cosine-squared', 'blackman'])
def env_window(request):
    return request.param


@pytest.fixture(scope='module', params=[5e-3, 0.1, 1, 10])
def env_duration(request):
    return request.param


@pytest.fixture(scope='module', params=[None, 0.25e-3, 2.5])
def env_rise_time(request):
    return request.param


def test_envelope(fs, env_window, env_duration, env_rise_time, chunksize,
                  n_chunks):
    if (env_rise_time is not None) and (env_duration < (env_rise_time * 2)):
        mesg = 'Rise time longer than envelope duration'
        with pytest.raises(ValueError):
            actual = stim.envelope(env_window, fs, duration=env_duration,
                                   rise_time=env_rise_time)
        return

    actual = stim.envelope(env_window, fs, duration=env_duration,
                           rise_time=env_rise_time)

    if env_rise_time is None:
        n_window = (len(actual) // 2) * 2
    else:
        n_window = int(round(env_rise_time * fs)) * 2
    n_steady_state = len(actual) - n_window

    if env_window == 'cosine-squared':
        # The scipy window function calculates the window points at the bin
        # centers, whereas my approach is to calculate the window points at the
        # left edge of the bin.
        expected = signal.windows.cosine(n_window) ** 2
        if n_steady_state != 0:
            expected = np.concatenate((
                expected[:n_window//2],
                np.ones(n_steady_state),
                expected[n_window//2:],
            ), axis=-1)
        assert_array_almost_equal(actual, expected, 1)
    else:
        expected = getattr(signal.windows, env_window)(n_window)
        if n_steady_state != 0:
            expected = np.concatenate((
                expected[:n_window//2],
                np.ones(n_steady_state),
                expected[n_window//2:],
            ), axis=-1)
        assert_array_equal(actual, expected)

    kwargs = {
        'envelope': env_window,
        'fs': fs,
        'duration': env_duration,
        'rise_time': env_rise_time,
        'input_factory': stim.SilenceFactory(fill_value=1),
    }
    assert_chunked_generation(stim.EnvelopeFactory, kwargs, chunksize,
                              n_chunks)
