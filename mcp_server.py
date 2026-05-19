"""MCP server — mp3 BPM sync 기능을 MCP 도구로 노출.

stdio 트랜스포트로 실행되며, Claude Code / Claude Desktop 등에서 호출 가능.

실행:
    python mcp_server.py
"""
from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sync_bpm import (
    detect_bpm_only,
    generate_kick_file,
    sync_mp3_file,
)

mcp = FastMCP("mp3-bpm-sync")


@mcp.tool()
def sync_mp3_to_bpm(
    mp3_path: str,
    target_bpm: float,
    merge_method: str = "hpf",
    merge_kick_gain: float = 0.5,
) -> dict:
    """mp3 를 target_bpm 으로 싱크하고 검증용 + kick 머지 스테레오 mp3 를 함께 생성.

    Args:
        mp3_path: 입력 mp3 파일 경로. 상대경로면 MCP 서버 cwd
            (= Claude Code 가 실행된 디렉터리) 기준.
        target_bpm: 목표 BPM (예: 170.0).
        merge_method: kick 과 synced 를 한 스테레오 mp3 로 합치는 방식.
            - 'hpf' (기본): synced 에 100Hz 하이패스 후 합 — kick 저음 자리 확보
            - 'simple'    : synced + kick*gain 단순 합
            - 'duck'      : 박마다 -6 dB 사이드체인 ducking — EDM 펌핑
        merge_kick_gain: 머지 시 kick 상대 게인 (기본 0.5, 원본=1.0).

    출력은 `<cwd>/output/` 에 저장됨.

    Returns:
        입력 길이/원본 BPM/검출된 비트 수/싱크 후 길이/출력 파일 경로 등을 담은 dict.
        - output_synced: target_bpm 으로 싱크된 스테레오 mp3 (320 kbps)
        - output_verify: L=기준 kick, R=싱크 결과 인 검증용 스테레오 mp3
        - output_merged: kick 과 synced 가 한 트랙에 믹스된 스테레오 mp3
    """
    p = Path(mp3_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        raise FileNotFoundError(f"mp3 파일을 찾을 수 없습니다: {p}")
    return sync_mp3_file(
        p,
        float(target_bpm),
        Path.cwd() / "output",
        merge_method=merge_method,
        merge_kick_gain=float(merge_kick_gain),
    )


@mcp.tool()
def detect_bpm(mp3_path: str, target_bpm_prior: float = 120.0) -> dict:
    """mp3 파일의 BPM 과 비트 정보만 검출 (파일 저장 없음).

    Args:
        mp3_path: 입력 mp3 파일 경로.
        target_bpm_prior: beat tracker 의 시작 추정값 + 옥타브 보정 기준. 기본 120.

    Returns:
        평균 BPM / raw BPM / 비트 개수 / 첫 비트 시각 / 구간별 BPM 통계 dict.
    """
    p = Path(mp3_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        raise FileNotFoundError(f"mp3 파일을 찾을 수 없습니다: {p}")
    return detect_bpm_only(p, float(target_bpm_prior))


@mcp.tool()
def generate_kick_track(
    target_bpm: float,
    duration_sec: float = 10.0,
) -> dict:
    """target_bpm 으로 이루어진 kick 드럼 트랙 mp3 생성.

    Args:
        target_bpm: 목표 BPM.
        duration_sec: 트랙 길이(초). 박 정수배로 자동 보정되어 seamless loop 됨.

    출력은 `<cwd>/output/` 에 저장됨 (= Claude Code 가 실행된 디렉터리).

    Returns:
        실제 트랙 길이와 출력 파일 절대 경로.
    """
    return generate_kick_file(float(target_bpm), float(duration_sec), Path.cwd() / "output")


if __name__ == "__main__":
    mcp.run()
