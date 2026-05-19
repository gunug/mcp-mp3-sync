# mcp_mp3_sync

**BPM 이 불확실하거나 곡 내내 흔들리는 mp3** 를 **원하는 BPM 그리드에 강제로 맞추고**,
**kick 드럼을 얹어 템포감이 확실한 mp3** 로 만들어주는 도구.

CLI 와 MCP 서버 두 방식으로 사용할 수 있다.

## 만들어지는 파일 (한 곡 입력 → 3개 산출)

| 산출 | 설명 |
| --- | --- |
| `*_synced_{bpm}bpm.mp3` | 원곡을 목표 BPM 그리드에 정렬한 결과 (피치 보존, 스테레오 유지) |
| `*_verify_{bpm}bpm.mp3` | L = kick / R = 결과. 헤드폰으로 들으면 그리드가 맞았는지 즉시 확인 |
| `*_merged_{method}_{bpm}bpm.mp3` | **메인 산출물** — kick 과 결과를 한 트랙에 합친 스테레오 mp3 |

## 주요 기능

- **BPM 검출** — librosa beat tracker + 옥타브 자동 보정 (raw 가 절반/두 배로 락되는 케이스 보정)
- **가변 BPM 정렬** — 한 곡 안에서 템포가 흔들려도 비트 구간마다 독립 time-stretch (pyrubberband, 피치 유지)
- **kick 머지** — `hpf` / `simple` / `duck` 세 가지 믹싱 방식으로 템포감 강한 결과물 출력

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
| `{name}_merged_{method}_{bpm}bpm.mp3` | kick + synced 가 한 트랙에 믹스된 스테레오 결과 |

### 머지 (kick + synced) 옵션

`--merge-method` 로 kick 을 synced 위에 얹는 방식을 선택할 수 있다.

| method | 동작 | 적합한 상황 |
| --- | --- | --- |
| `hpf` (기본) | synced 에 100 Hz 하이패스 → kick 의 저음 자리 확보 후 합 | 일반적 / 보컬·악기 위주 곡 |
| `simple` | synced + kick × gain 단순 합 | 빠른 디버깅, 저음 충돌 신경 안 쓸 때 |
| `duck` | 박마다 -6 dB 사이드체인 ducking (5/30/120 ms attack/hold/release) | EDM 펌핑 / 그루브 강조 |

`--merge-kick-gain` (기본 0.5) 로 kick 의 상대 음량 조정. 1.0 = 원본 그대로.

```powershell
python sync_bpm.py --bpm 170 --merge-method duck --merge-kick-gain 0.6
```

## MCP 서버로 사용

이 프로젝트는 [Model Context Protocol](https://modelcontextprotocol.io/) 서버를 포함하고 있어
**Claude Code / Claude Desktop 에 등록해두면 자연어로 "이 mp3 BPM 맞춰줘" 라고 부탁할 수 있다.**

### 노출되는 도구

| 도구 | 인자 | 용도 |
| --- | --- | --- |
| `sync_mp3_to_bpm` | `mp3_path, target_bpm, merge_method="hpf", merge_kick_gain=0.5` | 메인. 싱크 + 검증 + kick 머지 mp3 모두 생성 |
| `detect_bpm` | `mp3_path, target_bpm_prior=120.0` | 파일 저장 없이 BPM/비트 정보만 |
| `generate_kick_track` | `target_bpm, duration_sec=10.0` | 기준 BPM kick 트랙 mp3 단독 생성 |

각 도구는 결과 파일 절대 경로, 원본/검출 BPM, 비트 수, 첫 비트 시각, 구간별 BPM 통계를 dict 로 반환한다.
출력 파일은 모두 **MCP 서버를 실행한 cwd 의 `output/` 폴더** 에 저장된다.

### 등록 — Claude Code

CLI 한 줄 (user 스코프 = 모든 프로젝트에서 사용):

```powershell
claude mcp add mp3-bpm-sync --scope user -- python c:/onethelab/project/mcp_mp3_sync/mcp_server.py
```

가상환경을 쓴다면 venv 의 python 으로:

```powershell
claude mcp add mp3-bpm-sync --scope user -- c:/onethelab/project/mcp_mp3_sync/.venv/Scripts/python.exe c:/onethelab/project/mcp_mp3_sync/mcp_server.py
```

특정 프로젝트에서만 쓰려면 `--scope project` 로 바꾸면 그 디렉터리에 `.mcp.json` 이 생성된다.

확인 / 해제:

```powershell
claude mcp list
claude mcp remove mp3-bpm-sync
```

설정 파일을 직접 만들어도 된다 — 프로젝트 루트의 `.mcp.json` 또는 `~/.claude.json` 의 `mcpServers`:

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

### 등록 — Claude Desktop

설정 파일 위치 (Windows): `%APPDATA%\Claude\claude_desktop_config.json`

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

수정 후 Claude Desktop 을 완전히 종료한 다음 재실행하면 도구 목록에 자동으로 뜬다.

### 서버 단독 실행 (디버깅용)

```powershell
python mcp_server.py
```

stdio 트랜스포트로 대기하므로, 정상이면 아무 출력 없이 멈춰 있다. `Ctrl+C` 로 종료.

### 사용 예시 — Claude 와의 대화

등록 후 Claude 와 평소처럼 대화하면 도구를 알아서 호출한다.

> 「`c:/music/track.mp3` BPM 얼마야?」
> → `detect_bpm` → 「평균 142.3 BPM, 비트 384 개, 첫 비트 0.34 s. 구간별 138 ~ 147 BPM 으로 약간 흔들립니다.」

> 「`c:/music/track.mp3` 170 BPM 으로 맞추고 kick 도 같이 얹어줘」
> → `sync_mp3_to_bpm("c:/music/track.mp3", 170)`
> → 「`output/track_merged_hpf_170bpm.mp3` 에 저장. 검증용은 `..._verify_..` 입니다.」

> 「EDM 펌핑 느낌 줘」
> → `sync_mp3_to_bpm(..., merge_method="duck")`

> 「kick 좀 더 세게」
> → `sync_mp3_to_bpm(..., merge_kick_gain=0.8)`

> 「128 BPM 짜리 kick 트랙만 15초 짜리로 하나 만들어줘」
> → `generate_kick_track(128, 15)`

응답 dict 의 `output_merged` 절대 경로가 일반적으로 듣고 싶은 결과 — kick 이 얹혀 템포감이 확실한 mp3 다.

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
