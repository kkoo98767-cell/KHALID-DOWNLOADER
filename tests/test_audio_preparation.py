import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import audio_fingerprint, mark_normal_playback, select_audio_stream


class AudioPreparationTests(unittest.TestCase):
    @patch('app.main.subprocess.run')
    def test_high_frame_rate_gets_verified_full_rate_playback_metadata(self, run):
        run.side_effect = [subprocess.CompletedProcess([], 0, 'updated', ''),
                           subprocess.CompletedProcess([], 0, '1\n', '')]
        mark_normal_playback(Path('source.mp4'), {'avg_frame_rate': '120/1'})
        self.assertEqual(run.call_count, 2)
        self.assertIn('-Keys:FullFrameRatePlaybackIntent=1', run.call_args_list[0].args[0])
        self.assertIn('-config', run.call_args_list[0].args[0])

    @patch('app.main.subprocess.run')
    def test_sixty_fps_does_not_need_album_metadata(self, run):
        mark_normal_playback(Path('source.mp4'), {'avg_frame_rate': '60/1'})
        run.assert_not_called()

    @patch('app.main.subprocess.run')
    def test_unverified_playback_intent_blocks_delivery(self, run):
        run.side_effect = [subprocess.CompletedProcess([], 0, 'updated', ''),
                           subprocess.CompletedProcess([], 0, '0\n', '')]
        with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
            mark_normal_playback(Path('source.mp4'), {'avg_frame_rate': '120/1'})

    @patch('app.main.decode_audio')
    def test_corrupt_default_track_falls_back_to_clean_track(self, decode):
        decode.side_effect = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]
        bad = {'index': 2, 'codec_type': 'audio', 'disposition': {'default': 1}}
        good = {'index': 1, 'codec_type': 'audio', 'disposition': {'default': 0}}
        selected = select_audio_stream(Path('source.mp4'), {'streams': [good, bad]})
        self.assertEqual(selected['index'], 1)
        self.assertEqual([call.args[1] for call in decode.call_args_list], [2, 1])

    @patch('app.main.decode_audio')
    def test_two_default_tracks_choose_first_readable_track(self, decode):
        decode.return_value = subprocess.CompletedProcess([], 0)
        streams = [{'index': i, 'codec_type': 'audio', 'disposition': {'default': 1}} for i in [2, 1]]
        self.assertEqual(select_audio_stream(Path('source.mp4'), {'streams': streams})['index'], 1)

    @patch('app.main.decode_audio')
    def test_all_damaged_audio_rejects_delivery(self, decode):
        decode.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaisesRegex(RuntimeError, 'source audio is damaged'):
            select_audio_stream(Path('source.mp4'), {'streams': [{'index': 1, 'codec_type': 'audio'}]})

    @patch('app.main.decode_audio')
    def test_source_without_audio_is_not_a_failure(self, decode):
        self.assertIsNone(select_audio_stream(Path('silent.mp4'), {'streams': [{'index': 0, 'codec_type': 'video'}]}))
        decode.assert_not_called()

    @patch('app.main.subprocess.run')
    def test_audio_hash_uses_encoded_packets_without_reencoding(self, run):
        expected = 'SHA256=' + 'a' * 64
        run.return_value = subprocess.CompletedProcess([], 0, expected + '\n', '')
        self.assertEqual(audio_fingerprint(Path('source.mp4'), 1), expected)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-c:a') + 1], 'copy')
        self.assertEqual(command[command.index('-map') + 1], '0:1')


if __name__ == '__main__':
    unittest.main()
