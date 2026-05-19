"""mp3 BPM sync — goal.md 구현

사용법:
    python sync_bpm.py [--bpm 170] [--input-dir mp3] [--output-dir output]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import imageio_ffmpeg
import librosa
import numpy as np
import soundfile as sf
from pydub import AudioSegment

# pydub 가 imageio-ffmpeg 의 ffmpeg 를 사용하도록 설정 (시스템 ffmpeg 불필요)
_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
AudioSegment.converter = _FFMPEG
AudioSegment.ffmpeg = _FFMPEG
AudioSegment.ffprobe = _FFMPEG

SR = 44100  # 작업 샘플레이트


def generate_kick(bpm: float, duration_sec: float, sr: int = SR) -> np.ndarray:
    """기준 bpm 의 kick 으로 이루어진 트랙 생성.

    길이는 박(beat) 의 정수배로 맞춰 seamless loop 가 되도록 한다.
    """
    beat_interval = 60.0 / bpm
    n_beats = max(1, round(duration_sec / beat_interval))
    total_samples = int(round(n_beats * beat_interval * sr))
    track = np.zeros(total_samples, dtype=np.float32)

    # kick 한 방: 60Hz 사인 + 빠른 피치 드롭 + 짧은 envelope
    kick_len = int(0.15 * sr)
    t = np.arange(kick_len) / sr
    freq = 150 * np.exp(-t * 25) + 50          # 150Hz → 50Hz 드롭
    phase = 2 * np.pi * np.cumsum(freq) / sr
    env = np.exp(-t * 18).astype(np.float32)
    click_len = int(0.003 * sr)
    click = np.zeros(kick_len, dtype=np.float32)
    click[:click_len] = np.random.uniform(-1, 1, click_len) * np.linspace(1, 0, click_len)
    kick = (np.sin(phase) * env + click * 0.3).astype(np.float32)
    kick = kick / np.max(np.abs(kick)) * 0.9

    for i in range(n_beats):
        start = int(round(i * beat_interval * sr))
        end = start + kick_len
        if end <= total_samples:
            track[start:end] += kick
        else:
            # 루프 경계를 넘어가는 꼬리는 트랙 앞쪽으로 wrap → seamless loop
            head = total_samples - start
            track[start:] += kick[:head]
            tail = kick_len - head
            track[:tail] += kick[head:]

    peak = np.max(np.abs(track))
    if peak > 0:
        track = track / peak * 0.9
    return track


def snap_octave(bpm: float, target: float, max_k: int = 3) -> tuple[float, int]:
    """raw bpm × 2^k 중 target 과의 stretch ratio 가 1.0 에 가장 가까워지는 옥타브로 snap.

    Returns (snapped_bpm, k).
        k>0  → song 1박당 kick 2^k 번 (half-time / quarter-time)
        k<0  → song 박이 kick 보다 2^|k| 배 빠름 (double-time)
        k=0  → 일반 동기
    경계 (target/√2 ≈ target*0.707) 에서 half-time 으로 넘어감.
    """
    if bpm <= 0:
        return bpm, 0
    k = int(round(float(np.log2(target / bpm))))
    k = max(-max_k, min(max_k, k))
    return bpm * (2 ** k), k


def detect_beats(
    y: np.ndarray, sr: int, target_bpm: float
) -> tuple[np.ndarray, float, float, int]:
    """전 비트 시각(sec) + 옥타브 snap bpm + raw bpm + 옥타브 k.

    snap 이 raw 보다 빠른 그리드면 비트 사이에 중간점 삽입,
    더 느린 그리드면 한 칸씩 솎아내어 비트 배열을 snap 옥타브에 맞춘다.
    """
    tempo, beat_frames = librosa.beat.beat_track(
        y=y, sr=sr, start_bpm=float(target_bpm), units="frames"
    )
    raw_bpm = float(np.atleast_1d(tempo)[0])
    snapped, k = snap_octave(raw_bpm, target_bpm)
    beats = librosa.frames_to_time(beat_frames, sr=sr)

    grid_bpm = raw_bpm
    while snapped > grid_bpm * 1.5 and len(beats) >= 2:
        mids = (beats[:-1] + beats[1:]) / 2
        beats = np.sort(np.concatenate([beats, mids]))
        grid_bpm *= 2
    while snapped < grid_bpm * 0.75 and len(beats) >= 2:
        beats = beats[::2]
        grid_bpm /= 2

    return beats, snapped, raw_bpm, k


def time_stretch(y: np.ndarray, rate: float) -> np.ndarray:
    """피치 유지하며 템포 변경. rate > 1 → 빨라짐."""
    if abs(rate - 1.0) < 1e-4:
        return y
    return librosa.effects.time_stretch(y=y, rate=rate)


def time_varying_stretch(
    y: np.ndarray, sr: int, beats_sec: np.ndarray, target_bpm: float
) -> np.ndarray:
    """비트 간격마다 독립 스트레치 → 가변 BPM 보정.

    출력 길이 = (N_segments) × target_interval 로 그리드에 완벽히 정렬.
    """
    target_interval = 60.0 / target_bpm
    target_n = int(round(target_interval * sr))

    # 마지막 비트 뒤 꼬리도 한 박으로 매핑하기 위해 가상 종료 비트 추가
    if len(beats_sec) >= 2:
        median_iv = float(np.median(np.diff(beats_sec)))
        last_virtual = min(beats_sec[-1] + median_iv, len(y) / sr)
        beats_ext = np.concatenate([beats_sec, [last_virtual]])
    else:
        beats_ext = beats_sec

    out_segments: list[np.ndarray] = []
    for i in range(len(beats_ext) - 1):
        start = int(round(beats_ext[i] * sr))
        end = int(round(beats_ext[i + 1] * sr))
        end = min(end, len(y))
        if end <= start:
            continue
        seg = y[start:end]
        src_dur = (end - start) / sr
        rate = src_dur / target_interval

        # 너무 비정상적인 간격(검출 오류) → 스트레치 없이 길이만 맞춤
        if rate < 0.5 or rate > 2.0 or len(seg) < 64:
            seg_out = seg
        else:
            try:
                seg_out = librosa.effects.time_stretch(y=seg, rate=rate)
            except Exception:
                seg_out = seg

        # 강제로 target_n 샘플로 맞춤 → 그리드 정렬 보장
        if len(seg_out) >= target_n:
            seg_out = seg_out[:target_n]
        else:
            seg_out = np.pad(seg_out, (0, target_n - len(seg_out)))
        out_segments.append(seg_out)

    if not out_segments:
        return y
    return np.concatenate(out_segments).astype(np.float32)


def align_first_beat(y: np.ndarray, src_first_beat_sec: float, sr: int) -> np.ndarray:
    """첫 비트가 0초가 되도록 앞을 잘라내거나(또는 무음 패딩)."""
    offset = int(round(src_first_beat_sec * sr))
    if offset > 0:
        return y[offset:]
    if offset < 0:
        return np.concatenate([np.zeros(-offset, dtype=y.dtype), y])
    return y


def to_int16(y: np.ndarray) -> np.ndarray:
    y = np.clip(y, -1.0, 1.0)
    return (y * 32767).astype(np.int16)


def write_mp3(path: Path, y: np.ndarray, sr: int, channels: int = 1) -> None:
    """np.float32 (mono) 또는 (samples, 2) 스테레오 → mp3 저장."""
    if y.ndim == 1:
        data = to_int16(y).tobytes()
    else:
        # interleave stereo
        data = to_int16(y).tobytes()
        channels = y.shape[1]
    seg = AudioSegment(
        data=data,
        sample_width=2,
        frame_rate=sr,
        channels=channels,
    )
    seg.export(str(path), format="mp3", bitrate="192k")


def detect_bpm_only(mp3_path: Path, target_bpm: float = 120.0) -> dict:
    """mp3 파일의 BPM/비트 정보만 검출 (파일 출력 없음).

    MCP/라이브러리 호출용 조용한 API.
    """
    mp3_path = Path(mp3_path)
    y, sr = librosa.load(str(mp3_path), sr=SR, mono=True)
    duration = len(y) / sr
    beats, avg_bpm, raw_bpm, k = detect_beats(y, sr, target_bpm)

    result = {
        "input": str(mp3_path.resolve()),
        "input_duration_sec": round(duration, 3),
        "sample_rate": sr,
        "target_bpm_prior": float(target_bpm),
        "src_avg_bpm": round(float(avg_bpm), 2),
        "src_raw_bpm": round(float(raw_bpm), 2),
        "octave_corrected": abs(raw_bpm - avg_bpm) > 0.5,
        "octave_k": int(k),
        "kicks_per_song_beat": float(2 ** k),
        "effective_song_bpm": round(float(target_bpm) / (2 ** k), 2),
        "n_beats": int(len(beats)),
        "first_beat_sec": round(float(beats[0]), 3) if len(beats) > 0 else None,
    }
    if len(beats) >= 2:
        local_bpms = 60.0 / np.diff(beats)
        result.update(
            local_bpm_min=round(float(local_bpms.min()), 2),
            local_bpm_max=round(float(local_bpms.max()), 2),
            local_bpm_std=round(float(local_bpms.std()), 3),
        )
    return result


def sync_mp3_file(mp3_path: Path, target_bpm: float, out_dir: Path) -> dict:
    """mp3 → target_bpm 싱크 + 검증용 스테레오 mp3 생성. 결과 dict 반환.

    print 하지 않는 조용한 API (MCP/라이브러리 호출용).
    """
    mp3_path = Path(mp3_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    y, sr = librosa.load(str(mp3_path), sr=SR, mono=True)
    duration = len(y) / sr

    beats, avg_bpm, raw_bpm, k = detect_beats(y, sr, target_bpm)

    if len(beats) < 2:
        rate = target_bpm / max(avg_bpm, 1.0)
        y_synced = time_stretch(y, rate)
        first_beat = 0.0
        local_stats = {"local_bpm_min": None, "local_bpm_max": None, "local_bpm_std": None}
        method = "global_stretch_fallback"
    else:
        local_bpms = 60.0 / np.diff(beats)
        local_stats = {
            "local_bpm_min": round(float(local_bpms.min()), 2),
            "local_bpm_max": round(float(local_bpms.max()), 2),
            "local_bpm_std": round(float(local_bpms.std()), 3),
        }
        first_beat = float(beats[0])
        y_aligned = align_first_beat(y, first_beat, sr)
        beats_aligned = beats - first_beat
        y_synced = time_varying_stretch(y_aligned, sr, beats_aligned, target_bpm)
        method = "per_beat_variable_stretch"

    synced_duration = len(y_synced) / sr
    stem = mp3_path.stem
    out_synced = out_dir / f"{stem}_synced_{int(target_bpm)}bpm.mp3"
    write_mp3(out_synced, y_synced, sr)

    kick = generate_kick(target_bpm, synced_duration, sr)
    n = min(len(kick), len(y_synced))
    stereo = np.stack([kick[:n], y_synced[:n]], axis=1)
    out_verify = out_dir / f"{stem}_verify_{int(target_bpm)}bpm.mp3"
    write_mp3(out_verify, stereo, sr, channels=2)

    return {
        "input": str(mp3_path.resolve()),
        "input_duration_sec": round(duration, 3),
        "target_bpm": float(target_bpm),
        "src_avg_bpm": round(float(avg_bpm), 2),
        "src_raw_bpm": round(float(raw_bpm), 2),
        "octave_corrected": abs(raw_bpm - avg_bpm) > 0.5,
        "octave_k": int(k),
        "kicks_per_song_beat": float(2 ** k),
        "effective_song_bpm": round(float(target_bpm) / (2 ** k), 2),
        "n_beats": int(len(beats)),
        "first_beat_sec": round(first_beat, 3),
        **local_stats,
        "synced_duration_sec": round(synced_duration, 3),
        "method": method,
        "output_synced": str(out_synced.resolve()),
        "output_verify": str(out_verify.resolve()),
    }


def generate_kick_file(target_bpm: float, duration_sec: float, out_dir: Path) -> dict:
    """기준 BPM kick mp3 단독 생성. 결과 dict 반환."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    kick = generate_kick(target_bpm, duration_sec)
    out_path = out_dir / f"kick_{int(target_bpm)}bpm.mp3"
    write_mp3(out_path, kick, SR)
    return {
        "target_bpm": float(target_bpm),
        "duration_sec": round(len(kick) / SR, 3),
        "output": str(out_path.resolve()),
    }


def process_one(mp3_path: Path, target_bpm: float, out_dir: Path) -> None:
    """CLI용 wrapper — sync_mp3_file 호출 후 결과를 print."""
    print(f"\n=== {mp3_path.name} ===")
    r = sync_mp3_file(mp3_path, target_bpm, out_dir)
    print(f"  로드: {r['input_duration_sec']:.2f}s @ {SR}Hz")

    if r["method"] == "global_stretch_fallback":
        print(f"  [!] 비트 검출 실패 ({r['n_beats']}개) → 글로벌 스트레치 fallback")
    else:
        octave_note = f" (raw {r['src_raw_bpm']:.2f} → 옥타브 ×{2**r['octave_k']:g})" if r["octave_corrected"] else ""
        print(
            f"  비트 {r['n_beats']}개 | 평균 {r['src_avg_bpm']:.2f} bpm{octave_note}"
            f" | 구간별 {r['local_bpm_min']:.1f}~{r['local_bpm_max']:.1f}"
            f" (σ={r['local_bpm_std']:.2f})"
        )
        k = r["octave_k"]
        if k > 0:
            print(
                f"  ▶ half-time ×{2**k}: song {r['effective_song_bpm']:.1f} bpm @ kick {r['target_bpm']:.0f} bpm"
                f" (song 1박당 kick {2**k}회)"
            )
        elif k < 0:
            print(
                f"  ▶ double-time ×{2**-k}: song {r['effective_song_bpm']:.1f} bpm @ kick {r['target_bpm']:.0f} bpm"
                f" (song {2**-k}박당 kick 1회)"
            )
        print(f"  첫 비트: {r['first_beat_sec']:.3f}s | 가변 스트레치 적용")

    print(f"  싱크 후 길이: {r['synced_duration_sec']:.2f}s")
    print(f"  저장: {Path(r['output_synced']).name}")
    print(f"  저장: {Path(r['output_verify']).name} (L=kick, R=synced)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bpm", type=float, default=170.0)
    ap.add_argument("--input-dir", default="mp3")
    ap.add_argument("--output-dir", default="output")
    args = ap.parse_args()

    in_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 기준 bpm kick 트랙도 한 번 저장 (10초)
    kick_standalone = generate_kick(args.bpm, 10.0)
    kick_path = out_dir / f"kick_{int(args.bpm)}bpm.mp3"
    write_mp3(kick_path, kick_standalone, SR)
    print(f"기준 kick 저장: {kick_path}")

    mp3_files = sorted(in_dir.glob("*.mp3"))
    if not mp3_files:
        print(f"[!] {in_dir} 에 mp3 파일이 없습니다.")
        return 1

    for p in mp3_files:
        process_one(p, args.bpm, out_dir)

    print(f"\n완료: {len(mp3_files)}개 처리됨 → {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
