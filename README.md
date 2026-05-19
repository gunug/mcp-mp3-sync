# mcp_mp3_sync

mp3 파일의 BPM 을 측정하고 **목표 BPM 에 정확히 그리드 정렬**해주는 도구.
CLI 와 MCP 서버 두 가지 방식으로 사용할 수 있다.

## 주요 기능

- **BPM 검출** — librosa beat tracker + 옥타브 자동 보정 (raw 가 절반/두 배로 락되는 케이스 보정)
- **가변 BPM 정렬** — 한 곡 안에서 템포가 흔들리는 경우, 비트 구간마다 독립적으로 time-stretch 해서 그리드에 완벽히 맞춤 (피치는 유지)
- **검증용 스테레오 mp3** — L 채널에 기준 BPM kick 드럼, R 채널에 싱크 결과를 배치 → 헤드폰으로 들으면 그리드가 맞았는지 즉시 확인 가능

## 설치

```powershell
# 가상환경 (선택)
python -m venv .venv
.venv\Scripts\Activate.ps1

# 의존성
pip install -r requirements.txt
```

`ffmpeg` 는 별도 설치 불필요 — `imageio-ffmpeg` 가 번들된 바이너리를 사용한다.

`pyrubberband` 는 wrapper 이므로 **`rubberband` CLI 바이너리** 가 PATH 에 있어야 한다 (Windows):

```powershell
scoop install rubberband   # https://breakfastquay.com/rubberband/ 공식 바이너리
```

## CLI 사용법

`mp3/` 디렉터리의 모든 mp3 를 처리해서 `output/` 에 저장.

```powershell
# 기본: 170 BPM 으로 mp3/*.mp3 전부 변환
python sync_bpm.py

# 옵션 지정
python sync_bpm.py --bpm 128 --input-dir my_songs --output-dir result
```

### 출력 파일

| 파일 | 설명 |
| --- | --- |
| `kick_{bpm}bpm.mp3` | 기준 BPM 의 kick 드럼 트랙 (10초) |
| `{name}_synced_{bpm}bpm.mp3` | 싱크된 결과 (스테레오 유지, 320 kbps) |
| `{name}_verify_{bpm}bpm.mp3` | L=kick, R=결과(mono 다운믹스) 인 스테레오 검증용 |

## MCP 서버로 사용

이 프로젝트는 [Model Context Protocol](https://modelcontextprotocol.io/) 서버를 포함하고 있다.
Claude Code, Claude Desktop 등에서 도구로 호출할 수 있다.

### 노출되는 도구

| 도구 | 설명 |
| --- | --- |
| `sync_mp3_to_bpm(mp3_path, target_bpm, output_dir="output")` | mp3 를 목표 BPM 으로 싱크하고 검증 mp3 까지 생성 |
| `detect_bpm(mp3_path, target_bpm_prior=120.0)` | 파일 저장 없이 BPM/비트 정보만 반환 |
| `generate_kick_track(target_bpm, duration_sec=10.0, output_dir="output")` | 기준 BPM kick 트랙 mp3 단독 생성 |

각 도구는 입력 길이, 원본/raw BPM, 비트 수, 첫 비트 시각, 구간별 BPM 통계, 출력 파일 절대 경로 등을 dict 로 반환한다.

### 서버 단독 실행 (디버깅용)

```powershell
python mcp_server.py
```

stdio 트랜스포트로 대기하므로, 정상이면 아무 출력 없이 멈춰 있다. `Ctrl+C` 로 종료.

### Claude Code 에 등록

#### 방법 1: CLI 명령어 (권장)

```powershell
# 프로젝트 디렉터리에 등록 (.mcp.json 생성)
claude mcp add mp3-bpm-sync --scope project -- python c:/onethelab/project/mcp_mp3_sync/mcp_server.py

# 또는 user 스코프 (모든 프로젝트에서 사용)
claude mcp add mp3-bpm-sync --scope user -- python c:/onethelab/project/mcp_mp3_sync/mcp_server.py
```

가상환경을 쓴다면 해당 python 경로를 지정:

```powershell
claude mcp add mp3-bpm-sync --scope user -- c:/onethelab/project/mcp_mp3_sync/.venv/Scripts/python.exe c:/onethelab/project/mcp_mp3_sync/mcp_server.py
```

등록 확인 / 제거:

```powershell
claude mcp list
claude mcp remove mp3-bpm-sync
```

#### 방법 2: 설정 파일 직접 편집

프로젝트 루트의 `.mcp.json` (또는 `~/.claude.json` 의 `mcpServers` 섹션):

```json
{
  "mcpServers": {
    "mp3-bpm-sync": {
      "command": "python",
      "args": ["c:/onethelab/project/mcp_mp3_sync/mcp_server.py"]
    }
  }
}
```

### Claude Desktop 에 등록

설정 파일 위치 (Windows):

```
%APPDATA%\Claude\claude_desktop_config.json
```

다음을 추가 (이미 다른 서버가 있으면 `mcpServers` 안에 키만 추가):

```json
{
  "mcpServers": {
    "mp3-bpm-sync": {
      "command": "python",
      "args": ["c:/onethelab/project/mcp_mp3_sync/mcp_server.py"]
    }
  }
}
```

수정 후 Claude Desktop 을 완전히 종료 후 재실행.

### 사용 예시 (Claude 와의 대화)

> "c:/music/track.mp3 의 BPM 좀 알려줘"
> → `detect_bpm` 호출 → "평균 142.3 BPM, 비트 384개..." 응답

> "이 파일을 128 BPM 으로 맞춰서 c:/out 에 저장해줘"
> → `sync_mp3_to_bpm` 호출 → 결과 파일 경로 반환

## 동작 원리 요약

1. `librosa.beat.beat_track` 으로 전체 비트 시각 + raw BPM 검출 (mono 다운믹스 사용)
2. raw 가 목표 BPM 의 절반이면 비트 사이에 중간점 삽입, 두 배면 한 칸씩 솎아냄 (옥타브 보정)
3. 첫 비트 위치를 0초로 잘라냄 (다운비트 정렬)
4. 비트 구간마다 **pyrubberband 로 독립 time-stretch** (스테레오 유지, 트랜지언트 보존)
5. 인접 segment 끼리 **5 ms equal-power 크로스페이드** 로 OLA 결합 → 경계 클릭 제거
6. 소프트 리미터 + 320 kbps MP3 인코딩, 검증용 스테레오 mp3 동시 출력

자세한 코드: [sync_bpm.py](sync_bpm.py)

## 프로젝트 구조

```
mcp_mp3_sync/
├─ sync_bpm.py         # 핵심 알고리즘 + CLI
├─ mcp_server.py       # MCP 서버 (FastMCP)
├─ requirements.txt
├─ goal.md             # 원본 요구사항
├─ mp3/                # 입력 (gitignore)
└─ output/             # 결과 (gitignore)
```
