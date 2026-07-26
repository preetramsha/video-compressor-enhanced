import json
import subprocess
import os
import re
import time
import src.globals as g
from math import ceil, floor
from PyQt6.QtCore import QThread, pyqtSignal


def get_video_length(file_path):
    cmd = [
        g.ffprobe_path,
        "-v",
        "quiet",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        file_path,
    ]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    output = subprocess.check_output(cmd, creationflags=creationflags)
    data = json.loads(output)

    if "format" in data:
        duration = data["format"].get("duration")
        return float(duration) if duration else 0

    return 0


def get_audio_bitrate(video_path):
    cmd = [
        g.ffprobe_path,
        "-v",
        "quiet",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=bit_rate",
        "-of",
        "json",
        video_path,
    ]

    # Run ffprobe and capture output
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    output = subprocess.check_output(cmd, creationflags=creationflags)
    data = json.loads(output)

    # Extract bitrate from JSON response
    if "streams" in data and len(data["streams"]) > 0:
        bitrate = data["streams"][0].get("bit_rate")
        return round(float(bitrate) / 1000) if bitrate else 0

    return 0


def calculate_video_bitrate(file_path, target_size_mb):
    v_len = get_video_length(file_path)
    print(f"Video duration: {v_len} seconds")
    a_rate = get_audio_bitrate(file_path)
    print(f"Audio Bitrate: {a_rate}k")
    total_bitrate = (target_size_mb * 8192.0 * 0.98) / (1.048576 * v_len) - a_rate
    return max(1, round(total_bitrate))


class CompressionThread(QThread):
    update_log = pyqtSignal(str)
    update_progress = pyqtSignal(int)
    completed = pyqtSignal()

    def __init__(self, target_percentage, use_gpu, one_pass, parent=None):
        super().__init__(parent)
        self.target_percentage = target_percentage
        self.use_gpu = use_gpu
        self.one_pass = one_pass
        self.process = None

    def detect_gpu_encoder(self):
        try:
            cmd = [g.ffmpeg_path, "-hide_banner", "-encoders"]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            output = subprocess.check_output(
                cmd, universal_newlines=True, creationflags=creationflags
            )

            if "h264_nvenc" in output:
                return "h264_nvenc"
            elif "h264_qsv" in output:  # Intel QuickSync
                return "h264_qsv"
            elif "h264_amf" in output:  # AMD
                return "h264_amf"
            else:
                return None

        except subprocess.CalledProcessError:
            return None

    def run_pass(self, file_path):
        import os
        original_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        target_size_mb = original_size_mb * (self.target_percentage / 100.0)
        
        video_rate = calculate_video_bitrate(file_path, target_size_mb)
        gpu_encoder = self.detect_gpu_encoder() if self.use_gpu else None
        file_name = os.path.basename(file_path)

        passes = 1 if self.one_pass else 2

        for i in range(passes):
            # Calculate total progress based on queue position and current pass
            total_steps = len(g.queue) * passes  # Total number of passes for all videos
            current_step = (
                len(g.completed) * passes
            ) + i  # Completed videos * passes + current pass
            progress_percentage = (current_step / total_steps) * 100
            self.update_progress.emit(int(progress_percentage))
            encoder_type = (
                f"GPU ({gpu_encoder})" if self.use_gpu and gpu_encoder else "CPU"
            )
            status_msg = f"""
[Compression Status]
File: {file_name}
Queue: {len(g.completed) + 1}/{len(g.queue)}
Pass: {i + 1}/{passes}
Target Size: {target_size_mb:.2f}MB ({self.target_percentage}%)
Bitrate: {video_rate}k
Encoder: {encoder_type}
"""

            # Rest of the existing code remains the same
            bitrate_str = f"{video_rate}k"
            file_name_without_ext, original_ext = os.path.basename(file_path).rsplit(
                ".", 1
            )
            output_path = os.path.join(
                g.output_dir, f"{file_name_without_ext}-compressed.{original_ext}"
            )
            print(f"New bitrate: {bitrate_str}")
            print(status_msg)

            # Base command arguments
            cmd_args = [
                f'"{g.ffmpeg_path}"',
                f'-i "{file_path}"',
                "-y",
                f"-b:v {bitrate_str}",
            ]

            if self.use_gpu and gpu_encoder:
                print("Using GPU")
                cmd_args.extend([f"-c:v {gpu_encoder}"])
            else:
                print("Using CPU")
                cmd_args.extend(["-c:v libx264"])

            if passes == 1:
                cmd_args.extend([f'"{output_path}"'])
            elif i == 0:
                cmd_args.extend(["-an", "-pass 1", "-f mp4 TEMP"])
            else:
                cmd_args.extend(["-pass 2", f'"{output_path}"'])

            # Build argument list for subprocess (avoid a shell and quoted strings)
            # cmd_args currently contains individual arg strings; some may include
            # items like '"{path}"' or combined flags. Normalize into a list.
            normalized = []
            for a in cmd_args:
                if a.startswith('"') and a.endswith('"'):
                    normalized.append(a[1:-1])
                else:
                    parts = a.split()
                    for part in parts:
                        if part.startswith('"') and part.endswith('"'):
                            normalized.append(part[1:-1])
                        else:
                            normalized.append(part)

            # Prevent creating a console window on Windows
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            self.update_log.emit(status_msg)

            try:
                # Start ffmpeg and capture stderr (ffmpeg prints progress to stderr)
                self.process = subprocess.Popen(
                    normalized,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    creationflags=creationflags,
                )

                # Get total duration to compute percentage progress
                total_duration = get_video_length(file_path)

                # Read stderr lines and parse progress indicators
                stderr_errors = []
                for line in self.process.stderr:
                    if not line:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    if line == "progress=continue" or line == "progress=end":
                        continue

                    # Parse fps
                    fps_match = re.search(r"fps=\s*([0-9.]+)", line)
                    fps = fps_match.group(1) if fps_match else None

                    # Parse speed (e.g. 1.23x)
                    speed_match = re.search(r"speed=\s*([0-9.]+x)", line)
                    speed = speed_match.group(1) if speed_match else None

                    # Parse time (HH:MM:SS.xxx) - matches both "time=" and "out_time="
                    time_match = re.search(r"(?:out_)?time=\s*([0-9:.]+)", line)
                    elapsed_seconds = None
                    if time_match:
                        t = time_match.group(1)
                        parts = t.split(':')
                        try:
                            if len(parts) == 3:
                                h, m, s = parts
                                elapsed_seconds = float(h) * 3600 + float(m) * 60 + float(s)
                            elif len(parts) == 2:
                                m, s = parts
                                elapsed_seconds = float(m) * 60 + float(s)
                            else:
                                elapsed_seconds = float(parts[0])
                        except Exception:
                            elapsed_seconds = None

                    has_progress = fps_match or speed_match or time_match

                    # Show non-progress stderr lines (warnings/errors)
                    if not has_progress:
                        stderr_errors.append(line)
                        continue

                    # Compute combined progress across queue and passes
                    total_steps = len(g.queue) * passes
                    current_step = (len(g.completed) * passes) + i
                    base_progress = (current_step / total_steps) * 100 if total_steps > 0 else 0

                    additional = 0
                    if elapsed_seconds is not None and total_duration > 0:
                        # each pass for a single file accounts for 1/total_steps of overall work
                        additional = (elapsed_seconds / total_duration) * (1.0 / total_steps) * 100

                    overall_progress = min(100, base_progress + additional)

                    # Build a small progress snippet for the UI
                    prog_snip = ""
                    if time_match:
                        prog_snip += f"Time: {time_match.group(1)}  "
                    elapsed = time.time() - self.start_time
                    elapsed_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
                    prog_snip += f"Elapsed: {elapsed_str}  "
                    if fps:
                        prog_snip += f"FPS: {fps}  "
                    if speed:
                        prog_snip += f"Speed: {speed}  "

                    # Emit UI updates
                    if prog_snip:
                        self.update_log.emit(prog_snip)
                    self.update_progress.emit(int(overall_progress))

                rc = self.process.wait()
                if rc != 0:
                    if stderr_errors:
                        self.update_log.emit("\n".join(stderr_errors))
                    self.update_log.emit(f"ffmpeg exited with code {rc}")

            except Exception as e:
                self.update_log.emit(f"Error running ffmpeg: {e}")

    def run(self):
        self.start_time = time.time()
        g.completed = []

        for file_path in g.queue:
            if not g.compressing:
                break

            self.run_pass(file_path)
            g.completed.append(file_path)

        msg = (
            f"Compressed {len(g.completed)} video(s)!" if g.compressing else "Aborted!"
        )

        print(msg)
        self.update_log.emit(msg)
        self.completed.emit()
