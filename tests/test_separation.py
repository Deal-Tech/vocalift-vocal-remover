import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from app import main


class SuccessfulDemucs:
    stdout = ("Separating track input.wav\n", "100%\n")

    def wait(self):
        return 0


class SeparationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vocalift-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.job_id = uuid.uuid4().hex
        self.source = self.root / "input.wav"
        # Mirror RESULT_DIR/<job id> layout
        self.results = self.root / self.job_id
        model_dir = self.results / main.MODEL_NAME.replace("hf://", "").replace("/", "_")
        model_dir.mkdir(parents=True)
        sample_rate = 44100
        t = np.arange(sample_rate * 2, dtype=np.float32) / sample_rate
        vocal = 0.1 * np.sin(2 * np.pi * 440 * t)
        music = 0.1 * np.sin(2 * np.pi * 220 * t)
        self.frames = len(t)
        self.raw_vocals = model_dir / "vocals.wav"
        self.raw_instrumental = model_dir / "minus_vocals.wav"
        for path, audio in (
            (self.source, vocal + music),
            (self.raw_vocals, vocal),
            (self.raw_instrumental, music),
        ):
            sf.write(path, np.column_stack((audio, audio)), sample_rate, subtype="FLOAT")
        main.jobs[self.job_id] = {
            "id": self.job_id,
            "filename": "input.wav",
            "status": "queued",
            "progress": 0,
            "message": "",
        }
        self.addCleanup(main.jobs.pop, self.job_id, None)

    def run_with_demucs_output(self, refined_paths=None):
        real_popen = main.subprocess.Popen

        def start_process(command, *args, **kwargs):
            if command[:3] == [main.sys.executable, "-m", "demucs"]:
                return SuccessfulDemucs()
            return real_popen(command, *args, **kwargs)

        with (
            patch.object(main.subprocess, "Popen", side_effect=start_process),
            patch.object(
                main,
                "_refine_vocals",
                return_value=refined_paths,
                side_effect=RuntimeError("Cross-check failed") if refined_paths is None else None,
            ),
            patch.object(main, "_analyze_music", return_value={}),
        ):
            main._run_separation(self.job_id, self.source, self.results)
        return main.jobs[self.job_id]

    @unittest.skipUnless(main.FFMPEG_AVAILABLE, "FFmpeg is required for audio export")
    def test_failed_refinement_still_exports_playable_stems(self):
        with self.assertLogs(main.logger, level="WARNING"):
            job = self.run_with_demucs_output()
        self.assertEqual(job["status"], "completed", job["message"])
        self.assertEqual(job["progress"], 100)
        self.assertFalse(job["refinement_applied"])
        self.assertFalse(self.raw_vocals.exists())
        self.assertFalse(self.raw_instrumental.exists())
        public_job = main.get_job(self.job_id)
        for stem in ("vocals", "instrumental"):
            info = sf.info(job[stem])
            self.assertEqual(info.frames, self.frames)
            self.assertEqual(info.samplerate, 44100)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_24")
            for audio_format in ("wav", "mp3"):
                response = main.get_result(self.job_id, stem, audio_format)
                self.assertTrue(Path(response.path).is_file())
                self.assertGreater(public_job["downloads"][stem][audio_format]["bytes"], 100)
                self.assertEqual(response.media_type, "audio/wav" if audio_format == "wav" else "audio/mpeg")

    @unittest.skipUnless(main.FFMPEG_AVAILABLE, "FFmpeg is required for audio export")
    def test_finished_results_stay_downloadable_after_a_restart(self):
        with self.assertLogs(main.logger, level="WARNING"):
            self.run_with_demucs_output()
        # Simulate backend restart
        main.jobs.pop(self.job_id)
        with patch.object(main, "RESULT_DIR", self.root):
            public_job = main.get_job(self.job_id)
            self.assertEqual(public_job["status"], "completed")
            for stem in ("vocals", "instrumental"):
                self.assertEqual(set(public_job["downloads"][stem]), {"wav", "mp3"})
                for audio_format in ("wav", "mp3"):
                    response = main.get_result(self.job_id, stem, audio_format)
                    self.assertGreater(Path(response.path).stat().st_size, 100)

    def test_job_cut_off_by_a_restart_says_so(self):
        main.jobs.pop(self.job_id)
        with (
            patch.object(main, "RESULT_DIR", self.root),
            self.assertRaises(main.HTTPException) as caught,
        ):
            main.get_job(self.job_id)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("dimulai ulang", caught.exception.detail)

    def test_ffmpeg_processing_error_keeps_its_actual_message(self):
        error = "FFmpeg: Invalid audio data found when processing input."
        with (
            patch.object(main, "_master_stem", side_effect=RuntimeError(error)),
            self.assertLogs(main.logger, level="WARNING"),
        ):
            job = self.run_with_demucs_output()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["message"], error)
        self.assertTrue(self.raw_vocals.exists())

    @unittest.skipUnless(main.FFMPEG_AVAILABLE, "FFmpeg is required for audio export")
    def test_failed_mp3_encoding_keeps_playable_wav_results(self):
        with (
            patch.object(main, "_encode_mp3", side_effect=RuntimeError("MP3 encoder unavailable")),
            self.assertLogs(main.logger, level="WARNING"),
        ):
            job = self.run_with_demucs_output()
        self.assertEqual(job["status"], "completed", job["message"])
        self.assertEqual(job["progress"], 100)
        public_job = main.get_job(self.job_id)
        self.assertTrue(public_job["warnings"])
        for stem in ("vocals", "instrumental"):
            expected_url = f"/api/jobs/{self.job_id}/files/{stem}?format=wav"
            self.assertEqual(public_job["files"][stem], expected_url)
            self.assertEqual(set(public_job["downloads"][stem]), {"wav"})
            self.assertGreater(public_job["downloads"][stem]["wav"]["bytes"], 100)
            response = main.get_result(self.job_id, stem, "wav")
            audio, sample_rate = sf.read(response.path, dtype="float32", always_2d=True)
            self.assertEqual(audio.shape, (self.frames, 2))
            self.assertEqual(sample_rate, 44100)
            self.assertTrue(np.isfinite(audio).all())

    @unittest.skipUnless(main.FFMPEG_AVAILABLE, "FFmpeg is required for audio export")
    def test_one_failed_mp3_preserves_the_other_listening_copy(self):
        real_encode = main._encode_mp3

        def encode_mp3(source, destination, creation_flags):
            if destination.name == "vocals.mp3":
                destination.write_bytes(b"incomplete MP3")
                raise RuntimeError("Vocal MP3 encoding interrupted")
            return real_encode(source, destination, creation_flags)

        with (
            patch.object(main, "_encode_mp3", side_effect=encode_mp3),
            self.assertLogs(main.logger, level="WARNING"),
        ):
            job = self.run_with_demucs_output()
        self.assertEqual(job["status"], "completed", job["message"])
        public_job = main.get_job(self.job_id)
        self.assertTrue(public_job["warnings"])
        self.assertEqual(
            public_job["files"]["vocals"],
            f"/api/jobs/{self.job_id}/files/vocals?format=wav",
        )
        self.assertEqual(set(public_job["downloads"]["vocals"]), {"wav"})
        self.assertEqual(
            public_job["files"]["instrumental"],
            f"/api/jobs/{self.job_id}/files/instrumental",
        )
        self.assertEqual(set(public_job["downloads"]["instrumental"]), {"wav", "mp3"})
        response = main.get_result(self.job_id, "instrumental", "mp3")
        self.assertGreater(Path(response.path).stat().st_size, 100)
        self.assertEqual(response.media_type, "audio/mpeg")
        with self.assertRaises(main.HTTPException) as unavailable:
            main.get_result(self.job_id, "vocals", "mp3")
        self.assertEqual(unavailable.exception.status_code, 404)
        for stem in ("vocals", "instrumental"):
            self.assertEqual(sf.info(job[stem]).frames, self.frames)

    @unittest.skipUnless(main.FFMPEG_AVAILABLE, "FFmpeg is required for audio export")
    def test_locked_intermediate_does_not_discard_finished_exports(self):
        real_unlink = Path.unlink

        def remove_file(path, *args, **kwargs):
            if path in (self.raw_vocals, self.raw_instrumental):
                raise PermissionError("Temporary stem is still locked")
            return real_unlink(path, *args, **kwargs)

        with (
            patch.object(Path, "unlink", remove_file),
            self.assertLogs(main.logger, level="WARNING"),
        ):
            job = self.run_with_demucs_output()
        self.assertEqual(job["status"], "completed", job["message"])
        for stem in ("vocals", "instrumental"):
            self.assertEqual(sf.info(job[stem]).frames, self.frames)
            self.assertGreater(Path(job[f"{stem}_mp3"]).stat().st_size, 100)

    @unittest.skipUnless(main.FFMPEG_AVAILABLE, "FFmpeg is required for audio export")
    def test_locked_original_keeps_successful_refinement(self):
        refined_vocals = self.raw_vocals.with_name("vocals_refined.wav")
        refined_instrumental = self.raw_instrumental.with_name("instrumental_refined.wav")
        for original, refined in (
            (self.raw_vocals, refined_vocals),
            (self.raw_instrumental, refined_instrumental),
        ):
            refined.write_bytes(original.read_bytes())
        real_unlink = Path.unlink

        def remove_file(path, *args, **kwargs):
            if path == self.raw_instrumental:
                raise PermissionError("Original instrumental is still locked")
            return real_unlink(path, *args, **kwargs)

        with (
            patch.object(Path, "unlink", remove_file),
            self.assertLogs(main.logger, level="WARNING"),
        ):
            job = self.run_with_demucs_output((refined_vocals, refined_instrumental))
        self.assertEqual(job["status"], "completed", job["message"])
        self.assertTrue(job["refinement_applied"])
        self.assertFalse(self.raw_vocals.exists())
        for stem in ("vocals", "instrumental"):
            self.assertEqual(sf.info(job[stem]).frames, self.frames)

    def test_missing_ffmpeg_has_setup_instructions(self):
        error = FileNotFoundError(2, "No such file or directory", "ffmpeg")
        with (
            patch.object(main, "_master_stem", side_effect=error),
            self.assertLogs(main.logger, level="WARNING"),
        ):
            job = self.run_with_demucs_output()
        self.assertEqual(job["status"], "failed")
        self.assertIn("FFmpeg belum tersedia", job["message"])
        self.assertIn("server.ps1 -Setup", job["message"])

    def test_batched_reverb_fit_is_finite_and_does_not_add_energy(self):
        rng = np.random.default_rng(23)
        vocal = (rng.normal(size=(6, 128)) + 1j * rng.normal(size=(6, 128))).astype(np.complex64)
        vocal[:, 42:] = 0
        residual = (0.02 * (rng.normal(size=vocal.shape) + 1j * rng.normal(size=vocal.shape))).astype(np.complex64)
        residual[:, 4:] += 0.3 * vocal[:, :-4]
        with (
            patch.object(main, "INSTRUMENTAL_TAIL_TAPS", 14),
            patch.object(main, "INSTRUMENTAL_TAIL_RIDGE", 0.05),
            patch.object(main, "INSTRUMENTAL_TAIL_STRENGTH", 1.0),
        ):
            cleaned = main._cancel_vocal_tail(residual, vocal)
        self.assertEqual(cleaned.shape, residual.shape)
        self.assertTrue(np.isfinite(cleaned).all())
        self.assertGreater(np.max(np.abs(cleaned - residual)), 0)
        self.assertLessEqual(np.sum(np.abs(cleaned) ** 2), np.sum(np.abs(residual) ** 2) + 1e-5)


if __name__ == "__main__":
    unittest.main()
