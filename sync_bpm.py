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
import pyrubberband as pyrb
import soundfile as sf
from pydub import AudioSegment
from scipy import signal as _spsig

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


def compute_beat_reliability(beats: np.ndarray, threshold: float = 0.3) -> np.ndarray:
    """각 beat 구간의 신뢰도 플래그 (True=신뢰, False=오검출 의심).

    beat 간격의 중앙값 대비 편차가 threshold 초과하면 False.
    반환 길이: len(beats) - 1
    """
    if len(beats) < 2:
        return np.array([], dtype=bool)
    intervals = np.diff(beats)
    median_iv = float(np.median(intervals))
    if median_iv <= 0:
        return np.ones(len(intervals), dtype=bool)
    deviations = np.abs(intervals - median_iv) / median_iv
    return deviations <= threshold


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


def time_stretch(y: np.ndarray, rate: float, sr: int = SR) -> np.ndarray:
    """피치 유지하며 템포 변경. rate > 1 → 빨라짐. mono (n,) / stereo (n, c) 모두 OK.

    pyrubberband 사용 — librosa phase vocoder 대비 트랜지언트 보존 우수.
    """
    if abs(rate - 1.0) < 1e-4:
        return y.astype(np.float32, copy=False)
    return pyrb.time_stretch(y, sr, rate).astype(np.float32)


def time_varying_stretch(
    y: np.ndarray, sr: int, beats_sec: np.ndarray, target_bpm: float,
    reliable_flags: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """비트 간격마다 독립 스트레치 + 경계 OLA 크로스페이드.

    각 segment 를 target_n + crossfade_n 으로 stretch 한 뒤 인접 segment 와
    crossfade_n 만큼 오버랩 가산 → 그리드는 target_n 단위로 정렬되면서
    경계 위상 점프(클릭) 가 사라짐.

    reliable_flags: 각 beat 구간 신뢰도 (True=신뢰). None 이면 전부 신뢰.
    신뢰도 낮은 구간은 개별 rate 대신 median 기반 global rate 로 스트레치.

    Returns (output_audio, output_reliable_flags).
    """
    target_interval = 60.0 / target_bpm
    target_n = int(round(target_interval * sr))
    crossfade_n = max(8, int(round(0.005 * sr)))  # 5 ms equal-power crossfade
    extended_n = target_n + crossfade_n

    is_stereo = y.ndim == 2
    channels = y.shape[1] if is_stereo else 1
    n_total = y.shape[0]

    if len(beats_sec) >= 2:
        median_iv = float(np.median(np.diff(beats_sec)))
        last_virtual = min(beats_sec[-1] + median_iv, n_total / sr)
        beats_ext = np.concatenate([beats_sec, [last_virtual]])
        global_rate = median_iv * sr / extended_n
    else:
        beats_ext = beats_sec
        median_iv = target_interval
        global_rate = 1.0

    out_segments: list[np.ndarray] = []
    out_reliable: list[bool] = []
    for i in range(len(beats_ext) - 1):
        start = int(round(beats_ext[i] * sr))
        end = int(round(beats_ext[i + 1] * sr))
        end = min(end, n_total)
        if end <= start:
            continue
        seg = y[start:end]
        src_dur = (end - start) / sr

        is_reliable = True
        if reliable_flags is not None and i < len(reliable_flags):
            is_reliable = bool(reliable_flags[i])

        # 신뢰도 낮은 구간은 global rate (중앙값 BPM 기준) 사용
        rate = (global_rate if not is_reliable else src_dur * sr / extended_n)

        if rate < 0.5 or rate > 2.0 or (end - start) < 64:
            seg_out = seg.astype(np.float32, copy=False)
        else:
            try:
                seg_out = pyrb.time_stretch(seg, sr, rate).astype(np.float32)
            except Exception:
                seg_out = seg.astype(np.float32, copy=False)

        # 정확히 extended_n 으로 맞춤
        if seg_out.shape[0] >= extended_n:
            seg_out = seg_out[:extended_n]
        else:
            pad_width = [(0, extended_n - seg_out.shape[0])]
            if is_stereo:
                pad_width.append((0, 0))
            seg_out = np.pad(seg_out, pad_width)
        out_segments.append(seg_out)
        out_reliable.append(is_reliable)

    if not out_segments:
        empty = y.astype(np.float32, copy=False)
        return empty, np.array([], dtype=bool)

    n_segs = len(out_segments)
    total_len = n_segs * target_n + crossfade_n
    if is_stereo:
        result = np.zeros((total_len, channels), dtype=np.float32)
    else:
        result = np.zeros(total_len, dtype=np.float32)

    fade_in = np.sin(np.linspace(0.0, np.pi / 2.0, crossfade_n)).astype(np.float32)
    fade_out = np.cos(np.linspace(0.0, np.pi / 2.0, crossfade_n)).astype(np.float32)
    if is_stereo:
        fade_in_b = fade_in[:, None]
        fade_out_b = fade_out[:, None]
    else:
        fade_in_b = fade_in
        fade_out_b = fade_out

    for i, seg in enumerate(out_segments):
        s_pos = i * target_n
        e_pos = s_pos + seg.shape[0]
        s = seg.copy()
        if i > 0:
            s[:crossfade_n] *= fade_in_b
        if i < n_segs - 1:
            s[-crossfade_n:] *= fade_out_b
        result[s_pos:e_pos] += s

    return result, np.array(out_reliable, dtype=bool)


def make_synced_envelope(
    n_samples: int,
    sr: int,
    target_bpm: float,
    reliable_flags: np.ndarray,
    fade_beats: int = 2,
    low_gain: float = 0.6,
    start_fade_sec: float = 3.0,
    end_fade_sec: float = 3.0,
) -> np.ndarray:
    """synced 트랙에 적용할 per-sample gain envelope (kick 제외).

    - 신뢰도 낮은 beat 구간: low_gain, 전환 시 fade_beats 박 선형 페이드
    - 곡 시작 start_fade_sec 초: 0→1 fade in
    - 곡 끝 end_fade_sec 초: 1→0 fade out
    - 두 envelope 겹칠 때: 더 낮은 값 적용 (minimum)
    """
    target_n = int(round(60.0 / target_bpm * sr))
    fade_n = fade_beats * target_n
    n_segs = len(reliable_flags)

    # 신뢰도 기반 envelope
    rel_env = np.ones(n_samples, dtype=np.float32)
    for i, r in enumerate(reliable_flags):
        s = i * target_n
        e = min(s + target_n, n_samples)
        if not r:
            rel_env[s:e] = low_gain

    # 신뢰도 전환 경계 페이드 (경계 중심으로 fade_n 샘플 선형 보간)
    for i in range(n_segs - 1):
        g_cur = 1.0 if reliable_flags[i] else low_gain
        g_next = 1.0 if reliable_flags[i + 1] else low_gain
        if abs(g_cur - g_next) < 1e-6:
            continue
        boundary = (i + 1) * target_n
        fs = max(0, boundary - fade_n // 2)
        fe = min(n_samples, boundary + fade_n // 2)
        rel_env[fs:fe] = np.linspace(g_cur, g_next, fe - fs, dtype=np.float32)

    # 시작/끝 fade envelope
    se_env = np.ones(n_samples, dtype=np.float32)
    fi_n = min(int(round(start_fade_sec * sr)), n_samples)
    if fi_n > 0:
        se_env[:fi_n] = np.linspace(0.0, 1.0, fi_n, dtype=np.float32)
    fo_n = min(int(round(end_fade_sec * sr)), n_samples)
    if fo_n > 0:
        fade_out = np.linspace(1.0, 0.0, fo_n, dtype=np.float32)
        se_env[n_samples - fo_n:] = np.minimum(se_env[n_samples - fo_n:], fade_out)

    return np.minimum(rel_env, se_env)


def align_first_beat(y: np.ndarray, src_first_beat_sec: float, sr: int) -> np.ndarray:
    """첫 비트가 0초가 되도록 앞을 잘라내거나(또는 무음 패딩). mono/stereo 모두 OK."""
    offset = int(round(src_first_beat_sec * sr))
    if offset > 0:
        return y[offset:]
    if offset < 0:
        if y.ndim == 2:
            pad = np.zeros((-offset, y.shape[1]), dtype=y.dtype)
        else:
            pad = np.zeros(-offset, dtype=y.dtype)
        return np.concatenate([pad, y])
    return y


def _highpass(y: np.ndarray, sr: int, cutoff: float = 100.0, order: int = 4) -> np.ndarray:
    """Butterworth zero-phase HPF. y: (n,) or (n, channels) — axis 0 가 시간축."""
    sos = _spsig.butter(order, cutoff, btype="highpass", fs=sr, output="sos")
    return _spsig.sosfiltfilt(sos, y, axis=0).astype(np.float32)


def _make_duck_gain(
    n_samples: int,
    sr: int,
    target_bpm: float,
    depth_db: float = -6.0,
    attack_ms: float = 5.0,
    hold_ms: float = 30.0,
    release_ms: float = 120.0,
) -> np.ndarray:
    """그리드 박마다 -depth_db 만큼 잠시 죽이는 envelope.

    synced 가 이미 target_bpm 그리드에 정렬되어 있으므로 kick 의 envelope follower
    없이 비트 위치에 직접 ducking 커브를 깔면 끝.
    """
    gain = np.ones(n_samples, dtype=np.float32)
    beat_interval = 60.0 / target_bpm
    n_beats = int(np.floor(n_samples / sr / beat_interval)) + 2

    floor = float(10.0 ** (depth_db / 20.0))  # 0 < floor < 1
    attack_n = max(1, int(round(sr * attack_ms / 1000.0)))
    hold_n = max(1, int(round(sr * hold_ms / 1000.0)))
    release_n = max(1, int(round(sr * release_ms / 1000.0)))

    attack_curve = np.linspace(1.0, floor, attack_n, dtype=np.float32)
    release_curve = np.linspace(floor, 1.0, release_n, dtype=np.float32)

    for i in range(n_beats):
        beat_pos = int(round(i * beat_interval * sr))
        if beat_pos >= n_samples:
            break
        s = beat_pos
        e = min(s + attack_n, n_samples)
        gain[s:e] = np.minimum(gain[s:e], attack_curve[: e - s])
        s2, e2 = e, min(e + hold_n, n_samples)
        if e2 > s2:
            gain[s2:e2] = np.minimum(gain[s2:e2], floor)
        s3, e3 = e2, min(e2 + release_n, n_samples)
        if e3 > s3:
            gain[s3:e3] = np.minimum(gain[s3:e3], release_curve[: e3 - s3])
    return gain


MIX_METHODS = ("hpf", "simple", "duck")


def mix_kick_with_synced(
    synced: np.ndarray,
    kick: np.ndarray,
    sr: int,
    target_bpm: float,
    method: str = "hpf",
    kick_gain: float = 0.5,
    hpf_cutoff: float = 100.0,
    duck_depth_db: float = -6.0,
) -> np.ndarray:
    """synced(mono/stereo) + kick(mono) → 스테레오 믹스 (n, 2).

    method:
      'simple' — synced + kick*gain (단순 합)
      'hpf'    — synced 에 ``hpf_cutoff`` Hz HPF → kick 의 저음 자리 확보 후 합
      'duck'   — synced 에 박마다 ``duck_depth_db`` 사이드체인 ducking 후 합
    """
    if method not in MIX_METHODS:
        raise ValueError(f"unknown mix method: {method!r}; choose from {MIX_METHODS}")

    n = min(kick.shape[0], synced.shape[0])
    kick = kick[:n].astype(np.float32, copy=False) * float(kick_gain)
    s = synced[:n]

    # synced 를 (n, 2) stereo 로 정규화
    if s.ndim == 1:
        s_st = np.stack([s, s], axis=1).astype(np.float32, copy=False)
    elif s.shape[1] == 1:
        s_st = np.repeat(s, 2, axis=1).astype(np.float32, copy=False)
    else:
        s_st = s.astype(np.float32, copy=False)

    if method == "hpf":
        s_st = _highpass(s_st, sr, cutoff=hpf_cutoff)
    elif method == "duck":
        duck = _make_duck_gain(n, sr, target_bpm, depth_db=duck_depth_db)
        s_st = s_st * duck[:, None]

    kick_st = np.stack([kick, kick], axis=1)
    return (s_st + kick_st).astype(np.float32, copy=False)


def soft_limit(y: np.ndarray, ceiling: float = 0.98) -> np.ndarray:
    """피크가 ceiling 초과 시 균등 스케일 다운. 하드 클립 회피."""
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > ceiling:
        return (y * (ceiling / peak)).astype(np.float32, copy=False)
    return y.astype(np.float32, copy=False)


def to_int16(y: np.ndarray) -> np.ndarray:
    y = np.clip(y, -1.0, 1.0)
    return (y * 32767).astype(np.int16)


def write_mp3(path: Path, y: np.ndarray, sr: int, channels: int | None = None) -> None:
    """mono (n,) 또는 stereo (n, c) → soft-limit 후 320 kbps mp3 저장."""
    y = soft_limit(np.ascontiguousarray(y, dtype=np.float32))
    if y.ndim == 1:
        ch = 1 if channels is None else channels
    else:
        ch = y.shape[1]
    # numpy (n, c) 의 tobytes() 는 row-major → [L0,R0,L1,R1,...] interleave 와 일치
    data = to_int16(y).tobytes()
    seg = AudioSegment(
        data=data,
        sample_width=2,
        frame_rate=sr,
        channels=ch,
    )
    seg.export(str(path), format="mp3", bitrate="320k")


def detect_bpm_only(mp3_path: Path, target_bpm: float = 120.0) -> dict:
    """mp3 파일의 BPM/비트 정보만 검출 (파일 출력 없음).

    MCP/라이브러리 호출용 조용한 API.
    """
    mp3_path = Path(mp3_path)
    y_raw, sr = librosa.load(str(mp3_path), sr=SR, mono=False)
    y_mono = y_raw if y_raw.ndim == 1 else np.mean(y_raw, axis=0)
    duration = len(y_mono) / sr
    beats, avg_bpm, raw_bpm, k = detect_beats(y_mono, sr, target_bpm)

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
        reliable_flags = compute_beat_reliability(beats)
        n_reliable = int(np.sum(reliable_flags))
        n_unreliable = len(reliable_flags) - n_reliable
        result.update(
            local_bpm_min=round(float(local_bpms.min()), 2),
            local_bpm_max=round(float(local_bpms.max()), 2),
            local_bpm_std=round(float(local_bpms.std()), 3),
            median_beat_bpm=round(60.0 / float(np.median(np.diff(beats))), 2),
            n_reliable_beats=n_reliable,
            n_unreliable_beats=n_unreliable,
            unreliable_ratio=round(n_unreliable / max(len(reliable_flags), 1), 3),
        )
    return result


def sync_mp3_file(
    mp3_path: Path,
    target_bpm: float,
    out_dir: Path,
    merge_method: str = "hpf",
    merge_kick_gain: float = 0.5,
) -> dict:
    """mp3 → target_bpm 싱크 + 검증용 스테레오 mp3 + kick 머지 mp3 생성.

    Args:
        merge_method: 'hpf' | 'simple' | 'duck' — kick 과 synced 를 한 트랙에 합치는 방식.
        merge_kick_gain: 머지 시 kick 의 상대 게인 (1.0 = 원본 kick).

    print 하지 않는 조용한 API (MCP/라이브러리 호출용).
    """
    mp3_path = Path(mp3_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    y_raw, sr = librosa.load(str(mp3_path), sr=SR, mono=False)
    # 비트 검출은 mono 가 안정적, 실제 처리는 stereo 유지
    if y_raw.ndim == 1:
        y_mono = y_raw
        y_work = y_raw  # (n,)
    else:
        y_mono = np.mean(y_raw, axis=0)
        y_work = np.ascontiguousarray(y_raw.T)  # (n, channels)
    duration = len(y_mono) / sr

    beats, avg_bpm, raw_bpm, k = detect_beats(y_mono, sr, target_bpm)

    if len(beats) < 2:
        rate = target_bpm / max(avg_bpm, 1.0)
        y_synced = time_stretch(y_work, rate, sr)
        first_beat = 0.0
        local_stats = {"local_bpm_min": None, "local_bpm_max": None, "local_bpm_std": None}
        reliable_stats = {"n_reliable_beats": 0, "n_unreliable_beats": 0, "unreliable_ratio": 1.0}
        output_reliable = np.array([], dtype=bool)
        method = "global_stretch_fallback"
    else:
        local_bpms = 60.0 / np.diff(beats)
        local_stats = {
            "local_bpm_min": round(float(local_bpms.min()), 2),
            "local_bpm_max": round(float(local_bpms.max()), 2),
            "local_bpm_std": round(float(local_bpms.std()), 3),
        }
        first_beat = float(beats[0])
        y_aligned = align_first_beat(y_work, first_beat, sr)
        beats_aligned = beats - first_beat
        reliable_flags = compute_beat_reliability(beats_aligned)
        y_synced, output_reliable = time_varying_stretch(
            y_aligned, sr, beats_aligned, target_bpm, reliable_flags
        )
        n_reliable = int(np.sum(output_reliable))
        n_unreliable = len(output_reliable) - n_reliable
        reliable_stats = {
            "n_reliable_beats": n_reliable,
            "n_unreliable_beats": n_unreliable,
            "unreliable_ratio": round(n_unreliable / max(len(output_reliable), 1), 3),
        }
        method = "per_beat_variable_stretch"

    # synced 에 gain envelope 적용 (kick 제외)
    if len(output_reliable) > 0:
        envelope = make_synced_envelope(y_synced.shape[0], sr, target_bpm, output_reliable)
        if y_synced.ndim == 2:
            y_synced = (y_synced * envelope[:, None]).astype(np.float32)
        else:
            y_synced = (y_synced * envelope).astype(np.float32)
    else:
        # fallback: 시작/끝 fade 만 적용
        envelope = make_synced_envelope(
            y_synced.shape[0], sr, target_bpm,
            np.ones(max(1, y_synced.shape[0] // int(round(60.0 / target_bpm * sr))), dtype=bool),
        )
        if y_synced.ndim == 2:
            y_synced = (y_synced * envelope[:, None]).astype(np.float32)
        else:
            y_synced = (y_synced * envelope).astype(np.float32)

    synced_n = y_synced.shape[0]
    synced_duration = synced_n / sr
    stem = mp3_path.stem
    out_synced = out_dir / f"{stem}_synced_{int(target_bpm)}bpm.mp3"
    write_mp3(out_synced, y_synced, sr)

    # 머지 트랙은 kick 4박 배수 경계까지 연장 — synced 가 끝난 뒤 kick 만 이어 깔린다.
    beat_dur_sec = 60.0 / target_bpm
    n_synced_beats = max(1, int(round(synced_n / (beat_dur_sec * sr))))
    n_merged_beats = max(4, ((n_synced_beats + 3) // 4) * 4)
    merged_total_sec = n_merged_beats * beat_dur_sec

    # kick 은 연장 길이만큼 한 번에 생성해서 위상 연속성 확보
    kick = generate_kick(target_bpm, merged_total_sec, sr)
    kick_n = kick.shape[0]

    # verify: L = kick (mono), R = synced (stereo → mono mix). 길이는 synced 기준.
    synced_for_verify = (
        np.mean(y_synced, axis=1) if y_synced.ndim == 2 else y_synced
    )
    n_verify = min(kick_n, len(synced_for_verify))
    stereo = np.stack([kick[:n_verify], synced_for_verify[:n_verify]], axis=1)
    out_verify = out_dir / f"{stem}_verify_{int(target_bpm)}bpm.mp3"
    write_mp3(out_verify, stereo, sr)

    # merged: kick + synced 믹스 후, synced 가 끝난 구간은 kick 만 이어 붙인다.
    merged_main = mix_kick_with_synced(
        synced=y_synced,
        kick=kick,
        sr=sr,
        target_bpm=target_bpm,
        method=merge_method,
        kick_gain=merge_kick_gain,
    )
    n_mixed = merged_main.shape[0]
    if kick_n > n_mixed:
        tail = kick[n_mixed:] * float(merge_kick_gain)
        tail_st = np.stack([tail, tail], axis=1).astype(np.float32)
        merged = np.concatenate([merged_main, tail_st], axis=0).astype(np.float32)
    else:
        merged = merged_main
    merged_duration_sec = merged.shape[0] / sr
    tail_only_sec = max(0.0, merged_duration_sec - synced_duration)

    out_merged = out_dir / f"{stem}_merged_{merge_method}_{int(target_bpm)}bpm.mp3"
    write_mp3(out_merged, merged, sr)

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
        **reliable_stats,
        "synced_duration_sec": round(synced_duration, 3),
        "method": method,
        "merge_method": merge_method,
        "merge_kick_gain": float(merge_kick_gain),
        "n_synced_beats": int(n_synced_beats),
        "n_merged_beats": int(n_merged_beats),
        "merged_duration_sec": round(merged_duration_sec, 3),
        "tail_kick_only_sec": round(tail_only_sec, 3),
        "output_synced": str(out_synced.resolve()),
        "output_verify": str(out_verify.resolve()),
        "output_merged": str(out_merged.resolve()),
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


def process_one(
    mp3_path: Path,
    target_bpm: float,
    out_dir: Path,
    merge_method: str = "hpf",
    merge_kick_gain: float = 0.5,
) -> None:
    """CLI용 wrapper — sync_mp3_file 호출 후 결과를 print."""
    print(f"\n=== {mp3_path.name} ===")
    r = sync_mp3_file(mp3_path, target_bpm, out_dir, merge_method, merge_kick_gain)
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
    print(
        f"  머지 길이: {r['merged_duration_sec']:.2f}s"
        f" ({r['n_synced_beats']}박 → 4배수 경계 {r['n_merged_beats']}박,"
        f" kick-only tail {r['tail_kick_only_sec']:.2f}s)"
    )
    print(f"  저장: {Path(r['output_synced']).name}")
    print(f"  저장: {Path(r['output_verify']).name} (L=kick, R=synced)")
    print(
        f"  저장: {Path(r['output_merged']).name}"
        f" (mix={r['merge_method']}, kick_gain={r['merge_kick_gain']:g})"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bpm", type=float, default=170.0)
    ap.add_argument("--input-dir", default="mp3")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument(
        "--merge-method",
        choices=MIX_METHODS,
        default="hpf",
        help="kick 과 synced 를 한 mp3 에 합치는 방식 (default: hpf)",
    )
    ap.add_argument(
        "--merge-kick-gain",
        type=float,
        default=0.5,
        help="머지 시 kick 상대 게인 (default: 0.5)",
    )
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
        process_one(p, args.bpm, out_dir, args.merge_method, args.merge_kick_gain)

    print(f"\n완료: {len(mp3_files)}개 처리됨 → {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
