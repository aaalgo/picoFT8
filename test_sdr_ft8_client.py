"""Pipeline tests use deterministic decoder doubles; no radio is required."""
import argparse
import contextlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

import jt9
from sdr_ft8_client import (AudioChunk, DecoderConfig, FT8Decoder, SAMPLE_RATE,
                            MasterPoster, detection_for, main, parse_address, parse_utc,
                            print_detection)
from sdr_sources import EndReason, WavFileSource


class MasterTests(unittest.TestCase):
    def test_addresses_and_cli(self):
        for address in ('localhost:8074', '127.0.0.1:7777', '[::1]:7777'):
            self.assertEqual(parse_address(address), address)
        for address in ('localhost', 'host:0', 'host:65536', 'host:abc',
                        'http://host:7777', 'host:7777/path'):
            with self.assertRaises(argparse.ArgumentTypeError):
                parse_address(address)
        with patch('sdr_ft8_client.KiwiServerSource') as source:
            source.return_value.run.side_effect = lambda handler: None
            self.assertEqual(main(['-s', 'localhost:8074']), 0)
            self.assertEqual(source.call_args.args, ('localhost', 8074))
            handler = source.return_value.run.call_args.args[0]
            self.assertEqual(handler.on_batch.url, 'http://127.0.0.1:7777/api/rx/')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['-s', 'localhost:8074', '-p', '8074'])

    def test_posts_whole_batches_matching_printed_records(self):
        record = jt9.DecodeRecord('000000', -10, 0.0, 1000, '~', 'CQ TEST')
        poster = MasterPoster('localhost:7777', site_id=42)
        output = io.StringIO()
        with patch('sdr_ft8_client.urlopen') as post, contextlib.redirect_stdout(output):
            post.return_value.__enter__.return_value.status = 201
            handler = FT8Decoder(start_utc_ns=0,
                                 calibrate=lambda a, r: jt9.CalibrationResult(0, 0, []),
                                 decode=lambda a, r: [record, record],
                                 emit=lambda d: print_detection(d, 42), on_batch=poster)
            handler.on_start(SAMPLE_RATE)
            handler.on_data(np.zeros(30 * SAMPLE_RATE, dtype=np.int16))
            handler.on_end(EndReason.EOF)
        self.assertIsNone(handler.error)
        printed = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(post.call_count, 2)
        for index, call in enumerate(post.call_args_list):
            request = call.args[0]
            self.assertEqual(request.full_url, 'http://localhost:7777/api/rx/')
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(json.loads(request.data), printed[index * 2:index * 2 + 2])

    def test_failure_disables_posts_and_warns_once(self):
        chunk = AudioChunk(0, 0, 0, np.empty(0, dtype=np.int16))
        record = jt9.DecodeRecord('000000', -10, 0.0, 1000, '~', 'CQ TEST')
        batch = [detection_for(chunk, record, 'explicit')]
        for status in (None, 500):
            with self.subTest(status=status), patch('sdr_ft8_client.urlopen') as post:
                if status is None:
                    post.side_effect = OSError('connection refused')
                else:
                    post.return_value.__enter__.return_value.status = status
                poster = MasterPoster('localhost:7777')
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    poster([])
                    poster(batch)
                    poster(batch)
                self.assertTrue(poster.failed)
                self.assertEqual(post.call_count, 1)
                self.assertEqual(output.getvalue().count('Failed to post to master; not trying in future'), 1)
        with patch('sdr_ft8_client.urlopen') as post:
            MasterPoster(None)(batch)
            post.assert_not_called()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.stderr = contextlib.redirect_stderr(io.StringIO())
        self.stderr.__enter__()
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def test_retries_transition_and_exact_period_preserve_samples(self):
        attempts, decoded = [], []
        def calibrate(samples, rate):
            self.assertEqual(rate, 12000)
            attempts.append(len(samples))
            return jt9.CalibrationResult(2.25 if len(attempts) == 3 else None, 0, [])
        handler = FT8Decoder(start_utc_ns=0, calibrate=calibrate,
                             decode=lambda samples, rate: decoded.append(samples.copy()) or [])
        handler.on_start(12000.0)
        audio = (np.arange(50 * SAMPLE_RATE) % 32000).astype(np.int16)
        # Deliberately cross callback and chunk boundaries differently.
        for left, right in [(0, 20), (20, 25), (25, 30), (30, 32.25), (32.25, 50)]:
            handler.on_data(audio[round(left * SAMPLE_RATE):round(right * SAMPLE_RATE)])
        self.assertEqual(attempts, [20 * SAMPLE_RATE, 25 * SAMPLE_RATE, 30 * SAMPLE_RATE])
        self.assertEqual(handler.stage, 'DECODING')
        self.assertEqual(handler.next_chunk_index, 3)
        self.assertEqual(handler.buffered_samples, round(2.75 * SAMPLE_RATE))
        handler.on_end(EndReason.EOF)
        np.testing.assert_array_equal(np.concatenate(decoded), audio[27000:567000])
        self.assertIsNone(handler.error)

    def test_timestamps_round_slot_not_signal_start(self):
        chunk = AudioChunk(2, 180000, 7_200_000_000, np.empty(0, dtype=np.int16))
        record = jt9.DecodeRecord('000000', -10, 0.0, 1000, '~', 'CQ TEST')
        detection = detection_for(chunk, record, 'explicit')
        self.assertEqual(detection.recording_utc_ns, 7_700_000_000)
        self.assertEqual(detection.calibrated_utc_ns, 0)
        self.assertEqual(detection.delay_seconds, 7.2)
        self.assertEqual(parse_utc('1970-01-01T00:00:15Z'), 15_000_000_000)
        self.assertEqual(parse_utc('0'), 0)

    def test_delay_is_slot_residual_without_nominal_half_second(self):
        record = jt9.DecodeRecord('000000', -10, 0.0, 1000, '~', 'CQ TEST')
        for dt, expected in [(0.0, 0.0), (0.2, 0.2), (-0.3, -0.3)]:
            from dataclasses import replace
            chunk = AudioChunk(0, 0, 15_000_000_000, np.empty(0, dtype=np.int16))
            detection = detection_for(chunk, replace(record, dt_seconds=dt), 'callback_arrival')
            self.assertEqual(detection.calibrated_utc_ns, 15_000_000_000)
            self.assertEqual(detection.delay_seconds, expected)
            self.assertEqual(detection.recording_utc_ns - 500_000_000
                             - detection.calibrated_utc_ns, round(expected * 1_000_000_000))

    def test_live_clock_captured_once_and_sample_metadata(self):
        output = []
        record = jt9.DecodeRecord('000000', -1, .2, 100, '~', 'TEST')
        handler = FT8Decoder(clock=lambda: 100_000_000_000,
                             calibrate=lambda a, r: jt9.CalibrationResult(1, 0, []),
                             decode=lambda a, r: [record], emit=output.append)
        handler.on_start(12000)
        handler.on_data(np.zeros(20 * SAMPLE_RATE, dtype=np.int16))
        handler.clock = lambda: self.fail('Clock read more than once')
        handler.on_data(np.zeros(11 * SAMPLE_RATE, dtype=np.int16))
        handler.on_end(EndReason.EOF)
        self.assertEqual([d.chunk_start_sample for d in output], [12000, 192000])
        self.assertEqual([d.chunk_start_utc_ns for d in output],
                         [101_000_000_000, 116_000_000_000])
        self.assertEqual(output[0].recording_utc_ns, 101_700_000_000)

    def test_insufficient_audio_retries_but_other_errors_propagate(self):
        def insufficient(a, r):
            raise jt9.InsufficientAudioError('short')
        handler = FT8Decoder(calibrate=insufficient)
        handler.on_start(12000)
        handler.on_data(np.zeros(20 * SAMPLE_RATE, dtype=np.int16))
        self.assertEqual(handler.next_calibration_seconds, 25)
        handler.on_end(EndReason.EOF)
        self.assertIsNotNone(handler.error)
        def invalid(a, r):
            raise ValueError('invalid audio')
        handler = FT8Decoder(calibrate=invalid)
        handler.on_start(12000)
        try:
            with self.assertRaisesRegex(ValueError, 'invalid audio'):
                handler.on_data(np.zeros(20 * SAMPLE_RATE, dtype=np.int16))
        finally:
            handler.on_end(EndReason.ERROR)

    def test_calibration_limit_and_replay_backpressure(self):
        handler = FT8Decoder(DecoderConfig(calibration_max_seconds=20),
                             calibrate=lambda a, r: jt9.CalibrationResult(None, None, []))
        handler.on_start(12000)
        try:
            with self.assertRaisesRegex(RuntimeError, 'duration limit'):
                handler.on_data(np.zeros(20 * SAMPLE_RATE, dtype=np.int16))
        finally:
            handler.on_end(EndReason.ERROR)
        decoded = []
        handler = FT8Decoder(DecoderConfig(queue_size=1), start_utc_ns=0,
                             calibrate=lambda a, r: jt9.CalibrationResult(0, 0, []),
                             decode=lambda a, r: decoded.append(len(a)) or [])
        handler.on_start(12000)
        handler.on_data(np.zeros(90 * SAMPLE_RATE, dtype=np.int16))
        handler.on_end(EndReason.EOF)
        self.assertEqual(decoded, [180000] * 6)

    def test_worker_failure_stops_source_and_shutdown_does_not_deadlock(self):
        import threading
        stopped = threading.Event()
        def fail(a, r):
            raise RuntimeError('decode failed')
        handler = FT8Decoder(calibrate=lambda a, r: jt9.CalibrationResult(0, 0, []),
                             decode=fail, stop_source=stopped.set)
        handler.on_start(12000)
        handler.on_data(np.zeros(20 * SAMPLE_RATE, dtype=np.int16))
        self.assertTrue(stopped.wait(2))
        handler.on_end(EndReason.STOPPED)
        self.assertEqual(str(handler.error), 'decode failed')

    def test_live_queue_full_fails_without_dropping_silently(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def decode(a, r):
            entered.set()
            release.wait(2)
            return []
        handler = FT8Decoder(DecoderConfig(queue_size=1), live=True,
                             calibrate=lambda a, r: jt9.CalibrationResult(0, 0, []), decode=decode)
        handler.on_start(12000)
        try:
            handler.on_data(np.zeros(20 * SAMPLE_RATE, dtype=np.int16))
            self.assertTrue(entered.wait(2))
            handler.on_data(np.zeros(10 * SAMPLE_RATE, dtype=np.int16))
            with self.assertRaisesRegex(RuntimeError, 'queue is full'):
                handler.on_data(np.zeros(15 * SAMPLE_RATE, dtype=np.int16))
        finally:
            release.set()
            handler.on_end(EndReason.ERROR)

    def test_wav_native_rate_and_sample_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.wav'
            recorded = Path(directory) / 'output.wav'
            audio = (np.arange(31 * SAMPLE_RATE + 1100) % 65536 - 32768).astype(np.int16)
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 12000, 0, 'NONE', 'not compressed'))
                wav.writeframes(audio.astype('<i2').tobytes())
            decoded = []
            handler = FT8Decoder(start_utc_ns=0, record_path=recorded,
                                 calibrate=lambda a, r: jt9.CalibrationResult(1, 0, []),
                                 decode=lambda a, r: decoded.append(a.copy()) or [])
            WavFileSource(path, block_samples=997).run(handler)
            np.testing.assert_array_equal(np.concatenate(decoded), audio[SAMPLE_RATE:31 * SAMPLE_RATE])
            self.assertEqual(recorded.read_bytes(), path.read_bytes())
            self.assertEqual(handler.start_utc_ns, 0)
            self.assertIsNone(handler.error)

    def test_recording_checkpoint_every_period_and_final_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'active.wav'
            handler = FT8Decoder(record_path=path,
                                 calibrate=lambda a, r: jt9.CalibrationResult(None, None, []))
            handler.on_start(12000)
            expected = b''
            try:
                with patch.object(handler._recording, 'writeframes',
                                  wraps=handler._recording.writeframes) as checkpoint:
                    for period in range(2):
                        for block in range(15):
                            audio = np.arange(SAMPLE_RATE, dtype='<i2').tobytes()
                            handler.on_data(np.frombuffer(audio, dtype=np.int16))
                            expected += audio
                            self.assertEqual(checkpoint.call_count,
                                             period + (block == 14))
                        with wave.open(str(path), 'rb') as wav:
                            self.assertEqual(wav.getnframes(), len(expected) // 2)
                            self.assertEqual(wav.readframes(wav.getnframes()), expected)
                        self.assertEqual(path.stat().st_size, 44 + len(expected))
                    tail = np.arange(997, dtype='<i2').tobytes()
                    handler.on_data(np.frombuffer(tail, dtype=np.int16))
                    expected += tail
                    self.assertEqual(checkpoint.call_count, 2)
            finally:
                handler.on_end(EndReason.STOPPED)
            with wave.open(str(path), 'rb') as wav:
                self.assertEqual(wav.getnframes(), len(expected) // 2)
                self.assertEqual(wav.readframes(wav.getnframes()), expected)

    def test_recording_finalized_on_stop_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'partial.wav'
            audio = np.array([-32768, 0, 32767], dtype=np.int16)
            for reason in (EndReason.STOPPED, EndReason.ERROR):
                handler = FT8Decoder(record_path=path)
                handler.on_start(12000)
                handler.on_data(audio)
                handler.on_end(reason, RuntimeError('source failed') if reason == EndReason.ERROR else None)
                with wave.open(str(path), 'rb') as wav:
                    self.assertEqual(wav.getnframes(), 3)
                    self.assertEqual(wav.getframerate(), 12000)
                    self.assertEqual(wav.readframes(3), audio.astype('<i2').tobytes())

    def test_cli_recording_and_input_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.wav'
            output = Path(directory) / 'output.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 12000, 0, 'NONE', 'not compressed'))
                wav.writeframes(np.arange(1100, dtype='<i2').tobytes())
            original = path.read_bytes()
            # Short WAV cannot calibrate, but all received audio must be saved.
            self.assertEqual(main(['-f', str(path), '--record', str(output)]), 1)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(main(['-f', str(path), '--record', str(path)]), 1)
            self.assertEqual(path.read_bytes(), original)
            alias = Path(directory) / 'alias.wav'
            alias.hardlink_to(path)
            self.assertEqual(main(['-f', str(path), '--record', str(alias)]), 1)
            self.assertEqual(path.read_bytes(), original)

    def test_rate_rejected_and_cli_missing_file_fails(self):
        with self.assertRaises(AssertionError):
            FT8Decoder().on_start(11025)
        self.assertEqual(main(['-f', '/nonexistent/ft8.wav']), 1)
        for args in (['-f', 'input.wav', '-p', '8074'],
                     ['-s', 'example.invalid', '--block-samples', '512']):
            with self.assertRaises(SystemExit) as raised:
                main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_nominal_server_rate_tolerates_fractional_reporting_error(self):
        handler = FT8Decoder()
        handler.on_start(11999.999984)
        handler.on_end(EndReason.STOPPED)
        for rate in (11999.9, 12000.1, float('nan'), float('inf')):
            with self.assertRaisesRegex(AssertionError, 'reported'):
                FT8Decoder().on_start(rate)

    def test_recording_metadata_round_trip_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'capture.wav'
            output = Path(directory) / 'replay.wav'
            anchor = 1790122829215688498
            handler = FT8Decoder(record_path=source, clock=lambda: anchor)
            handler.on_start(12000)
            handler.on_data(np.arange(1100, dtype=np.int16))
            handler.on_end(EndReason.STOPPED)
            metadata = json.loads(source.with_suffix('.json').read_bytes())
            self.assertEqual(metadata['start_utc_ns'], anchor)
            self.assertEqual(metadata['timestamp_source'], 'callback_arrival')
            # Preserve arbitrary extra fields and original whitespace as well.
            metadata['note'] = 'original capture'
            raw = json.dumps(metadata, separators=(',', ':')).encode() + b'\n\n'
            source.with_suffix('.json').write_bytes(raw)
            with patch('sdr_ft8_client.FT8Decoder', wraps=FT8Decoder) as constructor:
                self.assertEqual(main(['-f', str(source), '--record', str(output)]), 1)
                self.assertEqual(constructor.call_args.kwargs['start_utc_ns'], anchor)
            self.assertEqual(output.read_bytes(), source.read_bytes())
            self.assertEqual(output.with_suffix('.json').read_bytes(), raw)
            self.assertEqual(main(['-f', str(source), '--record', str(output), '--start-utc', '0']), 1)
            overridden = json.loads(output.with_suffix('.json').read_bytes())
            self.assertEqual(overridden['start_utc_ns'], 0)
            self.assertEqual(overridden['note'], 'original capture')
            self.assertEqual(overridden['timestamp_source'], 'explicit')
            self.assertEqual(source.with_suffix('.json').read_bytes(), raw)

    def test_invalid_metadata_and_sidecar_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.wav'
            output = Path(directory) / 'output.wav'
            with wave.open(str(source), 'wb') as wav:
                wav.setparams((1, 2, 12000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0')
            for raw in ('not json', '{"start_utc_ns": true}', '{"start_utc_ns": 1.5}'):
                source.with_suffix('.json').write_text(raw)
                self.assertEqual(main(['-f', str(source), '--record', str(output)]), 1)
                self.assertFalse(output.exists())
            sidecar = source.with_suffix('.json')
            sidecar.write_text('{"start_utc_ns": 0}')
            output.with_suffix('.json').hardlink_to(sidecar)
            self.assertEqual(main(['-f', str(source), '--record', str(output)]), 1)
            self.assertEqual(sidecar.read_text(), '{"start_utc_ns": 0}')
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
