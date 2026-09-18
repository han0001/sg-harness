---
task: multi-provider
date: 20260830
status: design
task_dir: 20260830_multi-provider
---

# SG Harness 멀티 프로바이더 설계

작성일: 2026-08-31

## 1. 목표와 범위

### 목표

SG Harness의 핵심 작업 흐름을 유지하면서 Claude Code와 Codex에서 사용할 수 있게 한다.

사용자는 호스트와 실행기를 명시적으로 선택할 수 있어야 한다.

- 호스트: 사용자가 플러그인과 대화하는 환경
- 실행기(runtime): 격리된 하위 작업을 실제로 수행하는 CLI

호스트와 실행기는 같은 제품일 필요가 없다. 예를 들어 Codex에서 SG Harness를 사용하면서 하위 작업은 Claude CLI로 실행할 수 있다.

### 완료 기준

- 같은 SG skill이 Claude Code와 Codex에서 발견되고 실행된다.
- 실행기는 --runtime claude 또는 --runtime codex로 선택한다.
- 기존 Claude 경로의 동작과 결과 형식을 깨뜨리지 않는다.
- Codex 실행도 기존 retry/state/git 흐름에 같은 결과 계약으로 연결된다.
- 계획 인터뷰는 외부 GrillMe 설치 없이 작동한다.
- 위험 명령 hook은 두 호스트에서 같은 정책을 적용한다.
- 지원 범위를 계약 테스트와 최소 smoke test로 검증한다.

### 범위 밖

- 임의의 모든 LLM 공급자 지원
- MCP 또는 자체 호스팅 모델 API 추가
- Claude/Codex의 모든 hook 이벤트 추상화
- 호스트별 UI를 동일하게 만들기
- 저장소 안에 개인용 Codex marketplace를 생성하기
- 공개 플러그인 디렉터리 배포

## 2. 설계 원칙

### 2.1 포트와 어댑터

공통 SG 흐름을 안쪽에 두고, 제품별 CLI 차이는 어댑터로 격리한다.

- 공통 코어: task, state, git, retry, verdict
- 포트: AgentRuntime
- 어댑터: ClaudeRuntime, CodexRuntime

새 실행기를 추가할 때 공통 코어를 수정하지 않는 것이 기준이다.

### 2.2 능력 감지 우선

제품 버전만으로 동작을 추정하지 않는다. 각 runtime의 preflight가 필요한 실행 파일과 옵션을 직접 확인하고, 지원하지 않으면 상태나 git을 변경하기 전에 실패한다.

버전 하한이 실제 CLI 호환성에 필요하면 해당 어댑터 내부에만 둔다.

### 2.3 이식 가능한 핵심, 선택적인 통합

핵심 workflow는 Agent Skills 형식의 SKILL.md와 저장소 파일만으로 동작해야 한다.

hook, subagent, marketplace는 사용성을 높이는 통합 기능이다. 이 기능이 없어도 계획과 실행의 핵심 경로가 막히지 않아야 한다.

### 2.4 명시적 선택

실행기 자동 감지는 하지 않는다.

- 기본값: claude
- 선택: --runtime claude 또는 --runtime codex

자동 감지는 설치 상태에 따라 결과가 달라지고, 장애 원인을 숨길 수 있다. 명시적 선택은 재현성과 디버깅 가능성을 우선한다.

### 2.5 작은 계약

공통 인터페이스에는 현재 필요한 최소 기능만 둔다. provider별 이벤트나 내부 JSON을 공통 모델에 그대로 노출하지 않는다.

## 3. 목표 구조

### 3.1 전체 경계

    사용자
      |
      v
    공통 SG skills
      |
      +-- 계획 인터뷰와 승인
      +-- task/state/git/retry
      |
      v
    AgentRuntime
      |
      +-- ClaudeRuntime -> claude CLI
      +-- CodexRuntime  -> codex exec

플러그인 패키징과 hook은 이 흐름의 바깥에서 호스트 연결을 담당한다.

### 3.2 공통으로 유지할 것

- skills/의 SG workflow
- task와 plan 문서 형식
- state와 resume 규칙
- git diff와 작업 범위 확인
- retry와 종료 판정
- 최종 AttemptResult 직렬화

### 3.3 runtime으로 이동할 것

현재 execute.py에 섞여 있는 다음 책임만 분리한다.

- 실행 파일과 기능 preflight
- 명령행 구성
- instruction 파일 선택
- 환경 변수 구성
- stdout/stderr와 이벤트 해석
- 최종 verdict 읽기

### 3.4 제안 파일 구조

기존 구조를 크게 바꾸지 않고 실행기 경계만 추가한다.

    scripts/
      execute.py
      runtimes/
        base.py
        claude.py
        codex.py
      schemas/
        verdict.schema.json
    skills/
      sg-plan/SKILL.md
      sg-execute-task/SKILL.md
    hooks/hooks.json
    .claude-plugin/plugin.json
    .codex-plugin/plugin.json

파일명은 구현 중 현재 구조와 충돌할 때 조정할 수 있지만, 책임 경계는 유지한다.

## 4. 공통 계약

### 4.1 AgentRuntime

AgentRuntime은 다음 최소 책임만 가진다.

| 항목 | 의미 |
|---|---|
| name | 결과와 로그에 기록할 실행기 이름 |
| instruction_files | 해당 실행기가 우선해서 읽을 지침 파일 |
| preflight | 실행 전에 CLI와 필수 능력 검증 |
| run | prompt와 작업 경로를 받아 AttemptResult 반환 |

프로세스 생성, 스트림 형식, provider별 오류 해석은 각 runtime 내부 책임이다.

### 4.2 AttemptResult

기존 필드명은 호환성을 위해 유지한다. saw_result_with_structured_output 같은 이름이 Claude 구현에서 유래했더라도 지금은 의미를 바꾸지 않는다.

추가할 공통 정보는 runtime 필드 하나로 제한한다.

- runtime: claude 또는 codex
- 기존 구조화 결과 여부 필드: 유효한 최종 verdict를 얻었는지로 해석

필드 이름 변경과 데이터 마이그레이션은 이번 작업에 포함하지 않는다.

### 4.3 최종 verdict

두 runtime은 같은 JSON Schema를 만족하는 verdict를 반환해야 한다. 공통 코어는 provider 이벤트가 아니라 이 verdict만 판정에 사용한다.

필수 상태는 현재 SG Harness가 사용하는 완료, 차단, 명시적 실패 범위를 유지한다. 새로운 상태는 실제 요구가 생기기 전까지 추가하지 않는다.

### 4.4 instruction 파일

한 실행에서 동일한 내용의 지침을 중복 주입하지 않는다.

| runtime | 우선순위 |
|---|---|
| Claude | CLAUDE.md, 없으면 AGENTS.md |
| Codex | AGENTS.md, 없으면 CLAUDE.md |

장기적으로 저장소 루트의 AGENTS.md를 짧은 공통 진입점으로 사용하고, 제품별 파일은 필요한 차이만 담는다.

### 4.5 오류와 timeout

공통 코어가 구분해야 하는 결과는 다음뿐이다.

- 정상 완료
- 사용자 입력이나 외부 조건으로 차단
- agent가 보고한 명시적 실패
- verdict 누락 또는 형식 오류
- CLI 비정상 종료
- 출력 없이 멈춘 idle timeout
- 전체 실행 wall timeout

세부 오류 메시지는 runtime이 보존하되, retry 여부는 기존 공통 정책이 결정한다.

## 5. 제품별 결정

### 5.1 ClaudeRuntime

현재 Claude 실행 경로를 그대로 옮기는 것이 첫 단계다.

- 현재 버전 검사 유지
- 현재 CLI 옵션 유지
- CLAUDE_SKILL_DIR 환경 변수와 permission 동작 유지
- 현재 stream parser 유지

이 단계에서 동작 변경을 만들지 않는다. 목적은 회귀 없는 경계 추출이다.

### 5.2 CodexRuntime

Codex는 비대화형 실행 계약을 사용한다.

    codex exec --json --ephemeral
      --sandbox workspace-write
      --output-schema <schema.json>
      -o <verdict.json>
      <prompt>

- --json: 진행 이벤트를 JSONL로 읽는다.
- --ephemeral: 하위 실행 세션을 저장하지 않는다.
- --sandbox workspace-write: 현재 작업 범위에서 수정할 수 있게 한다.
- --output-schema: 최종 결과 계약을 강제한다.
- -o: 최종 메시지를 provider 이벤트와 분리해 읽는다.

CodexRuntime은 승인 요청이 새로 필요하지 않도록 preflight에서 환경을 검증한다. 하위 비대화형 실행 중 새 승인이 필요하면 실패로 처리한다.

### 5.3 GrillMe와 계획 인터뷰

현재 sg-plan이 외부 /grill-me skill을 요구하는 것은 숨은 의존성이다. 이를 제거하고 필요한 대화 계약만 sg-plan에 포함한다.

계약은 다음 다섯 가지다.

1. 질문 전에 저장소와 기존 문서를 먼저 확인한다.
2. 의존 관계가 있는 결정부터 묻는다.
3. 한 번에 질문 하나와 권장안을 제시한다.
4. 선택한 이유와 trade-off를 계획에 기록한다.
5. 사용자가 설계를 승인한 뒤 구현 계획을 확정하며, 계획 단계에서 구현하지 않는다.

계획 인터뷰는 항상 main conversation에서 진행한다. 이는 사용자의 잦은 피드백과 공유 맥락이 필요하기 때문이다.

subagent는 독립적인 읽기 전용 코드 탐색에만 선택적으로 사용한다. 사용할 수 없으면 main agent가 같은 조사를 수행하므로 별도 delegation adapter나 custom agent 파일은 만들지 않는다.

### 5.4 hook

현재 hooks/hooks.json의 공통 부분을 유지한다.

- 이벤트: PreToolUse
- matcher: Bash
- 입력: tool_input.command
- 차단: stderr 메시지와 exit code 2

hook은 위험 명령을 한 번 더 막는 방어 계층이다. 핵심 workflow가 hook 실행에 의존해서는 안 된다.

우선 Claude와 Codex의 실제 payload fixture로 동일 script를 계약 테스트한다. 양쪽 계약이 실제로 달라질 때만 얇은 wrapper를 추가한다.

### 5.5 패키징

공통 skills, scripts, references, assets, hooks를 복제하지 않고 두 manifest가 같은 파일을 가리키게 한다.

- Claude: .claude-plugin/plugin.json
- Codex: .codex-plugin/plugin.json
- 두 manifest의 버전: 동일하게 관리

Codex 개인 marketplace 설정은 사용자 환경에 설치되는 정보이므로 저장소에 .agents/plugins/marketplace.json을 추가하지 않는다. 로컬 설치 절차만 문서화한다.

공개 디렉터리 제출은 기능 검증 이후의 별도 작업으로 둔다.

## 6. 구현과 검증

### 단계 1: Claude 경계 추출

- AgentRuntime 계약 추가
- execute.py의 Claude 전용 코드를 ClaudeRuntime으로 이동
- 기본 runtime을 claude로 연결

완료 기준: 기존 Claude 테스트와 fixture 결과가 변경 전과 동일하다.

### 단계 2: Codex runtime

- 공통 verdict JSON Schema 추가
- Codex preflight, command builder, 결과 parser 구현
- --runtime codex 연결

완료 기준: 같은 fixture task가 Codex에서 AttemptResult로 정규화된다.

### 단계 3: portable skills와 안전 계약

- sg-plan에서 /grill-me 외부 의존성 제거
- 위의 인터뷰 계약을 sg-plan에 포함
- sg-execute-task의 Claude 전용 표현을 runtime 중립적으로 변경
- hook payload fixture를 Claude와 Codex 양쪽에 추가

완료 기준: 공통 skill에 CLAUDE_SKILL_DIR 또는 Claude permission이 필수 전제로 남지 않고, 두 hook fixture가 같은 차단 결과를 낸다.

### 단계 4: 패키징과 smoke test

- Codex manifest 추가
- Claude/Codex 설치 문서 추가
- 각 호스트에서 plan 1회, 각 runtime에서 작은 task 1회 실행

완료 기준: 설치부터 계획, 실행, 결과 확인까지 문서만 보고 재현된다.

### 계약 테스트 행렬

| 사례 | ClaudeRuntime | CodexRuntime | 기대 결과 |
|---|---:|---:|---|
| 완료 verdict | 필수 | 필수 | 완료 |
| 차단 verdict | 필수 | 필수 | 차단 사유 보존 |
| 명시적 실패 | 필수 | 필수 | 실패 사유 보존 |
| verdict 누락/형식 오류 | 필수 | 필수 | runtime 오류 |
| CLI 비정상 종료 | 필수 | 필수 | 종료 코드 보존 |
| idle timeout | 필수 | 필수 | retry 정책 적용 |
| wall timeout | 필수 | 필수 | 강제 종료 후 실패 |

### 정적 검증

- manifest가 존재하고 같은 버전을 가리키는지 확인
- SKILL.md frontmatter와 상대 경로 확인
- Claude 전용 문자열이 공통 계층에 남았는지 검색
- runtime 선택 누락 시 claude가 기본인지 확인
- 기존 결과 JSON을 읽는 소비자와 호환되는지 확인

## 7. 근거 자료

설계 판단에 직접 사용한 자료만 남긴다.

- [Alistair Cockburn, Hexagonal Architecture](https://alistair.cockburn.us/hexagonal-architecture)
- [Agent Skills Specification](https://agentskills.io/specification)
- [Agent Skills Best Practices](https://agentskills.io/skill-creation/best-practices)
- [OpenAI, Convert a Claude Code plugin](https://developers.openai.com/plugins/guides/submit-claude-plugin)
- [OpenAI, Hooks in Codex](https://learn.chatgpt.com/docs/hooks)
- [OpenAI, Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI, Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [OpenAI, Build plugins](https://learn.chatgpt.com/docs/build-plugins)
- [OpenAI, Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Anthropic, Hooks reference](https://code.claude.com/docs/en/hooks)
- [Anthropic, Subagents](https://code.claude.com/docs/en/sub-agents)
- [GNU Autoconf, Versioning and feature checks](https://www.gnu.org/software/autoconf/manual/autoconf-2.65/html_node/Versioning.html)
- [Martin Fowler, Contract Test](https://martinfowler.com/bliki/ContractTest.html)

이 문서는 구현에 필요한 결정, 검증 기준, 새 세션의 출발점을 다룬다. 내부 helper와 코드 조각은 구현 단계에서 현재 코드에 맞춰 정한다.

## 8. 새 구현 세션 인계

이 절은 이전 대화가 없는 새 세션의 시작점이다. 구현자는 이 문서 전체를 읽고 아래 순서를 따른다. 코드가 문서의 행 번호와 달라졌다면 심볼 이름을 기준으로 찾고, 실제 코드가 최종 사실 원천이다.

### 8.1 시작 순서

1. 이 계획 문서 전체를 읽는다.
2. 아래 파일을 순서대로 읽어 현재 계약을 확인한다.
3. 기준 테스트를 실행한다.
4. 각 구현 단계에서 실패하는 테스트를 먼저 추가한 뒤 최소 변경으로 통과시킨다.
5. 한 단계의 전체 테스트가 통과하기 전에는 다음 단계로 넘어가지 않는다.

| 파일 | 먼저 확인할 내용 |
|---|---|
| CLAUDE.md | 용어, 저장소 구조, 불변 조건, 비목표 |
| skills/sg-execute-task/scripts/execute.py | VERDICT_SCHEMA, preflight_check, AttemptResult, run_child, StateStore, StepExecutor |
| skills/sg-execute-task/scripts/test_execute.py | 현재 168개 회귀 테스트와 mocking 방식 |
| skills/sg-plan/SKILL.md | /grill-me와 Explore agent 의존성 |
| skills/sg-execute-task/SKILL.md | CLAUDE_SKILL_DIR, Claude child, permission 관련 표현 |
| hooks/hooks.json | 현재 PreToolUse/Bash 차단 계약 |
| .claude-plugin/plugin.json, .claude-plugin/marketplace.json | 기존 Claude 배포 정보 |
| README.md, .github/workflows/test.yml | 사용자 여정과 CI 검증 명령 |

기준 명령:

    python3 -m pytest skills/sg-execute-task/scripts/test_execute.py -q

2026-08-31 기준 기대 결과는 168 passed다. 시작 시 실패하면 구현을 진행하기 전에 환경 문제인지 기존 회귀인지 기록한다. 저장소 문서의 .venv 명령은 현재 checkout에 .venv가 없으므로 기준 명령으로 사용하지 않는다.

확인 당시 로컬 CLI는 Claude Code 2.1.216과 codex-cli 0.151.0이었다. 이는 환경 기록일 뿐 Codex 버전 하한으로 사용하지 않는다. Codex 지원 여부는 필요한 옵션의 존재로 판정한다.

### 8.2 반드시 보존할 계약

- 작업 대상은 script 설치 위치가 아니라 cwd의 git root다.
- StateStore만 task index와 top index를 쓴다.
- StepExecutor만 branch, commit, 선택적 push를 수행한다. child runtime은 git 명령을 실행하지 않는다.
- step은 순차 실행하고 MAX_RETRIES=3, --once, --push, blocked exit 2, error exit 1 동작을 유지한다.
- 선택된 runtime의 preflight는 header, blocker 확인, git, state 변경보다 먼저 실행한다.
- 기본 runtime은 claude이며 현재 Claude 명령, 최소 버전 2.1.216, capability 검사, Bash timeout 환경 변수, stream parser 동작을 보존한다.
- CLI에는 --runtime claude 또는 --runtime codex만 허용하고 자동 감지하지 않는다.
- verdict 필드는 passed, summary, error, blocked, blocked_reason을 유지한다. passed는 필수이며 실패에는 error, 차단에는 blocked_reason이 필요하다.
- AttemptResult의 기존 필드는 삭제하거나 이름을 바꾸지 않는다. outcome, verdict, kill_reason, signal, elapsed, last_activity_age, return_code, stderr_tail, stdout_tail, saw_result_with_structured_output을 유지하고 runtime만 추가한다.
- step output JSON의 step, name, attempt와 기존 AttemptResult 필드를 유지하고 runtime을 추가한다.
- 공통 core는 StateStore, git, retry, 상태 판정을 소유한다. CLI 명령, preflight, instruction 선택, event parsing만 runtime이 소유한다.
- sg-plan은 외부 /grill-me나 subagent를 필수로 요구하지 않는다.
- hook이나 marketplace가 없어도 plan, decompose, execute의 핵심 workflow는 동작해야 한다.
- 이 작업과 무관한 용어 정리, 결과 필드 rename, git 구조 변경, 병렬 step 실행은 하지 않는다.

### 8.3 파일별 구현 지도

아래 경로는 구현의 기본 구조다. 기존 공개 동작을 보존하기 위해 execute.py가 현재 import되는 심볼을 다시 노출해야 한다면 얇은 re-export를 허용한다.

| 단계 | 변경 파일과 책임 |
|---|---|
| Claude 경계 | skills/sg-execute-task/scripts/execute.py: runtime 선택과 공통 orchestration만 유지 |
| Claude 경계 | skills/sg-execute-task/scripts/runtimes/base.py: AgentRuntime 계약과 공통 결과 타입 |
| Claude 경계 | skills/sg-execute-task/scripts/runtimes/claude.py: 현재 preflight, 명령, 환경, stream verdict 해석 이동 |
| Claude 경계 | skills/sg-execute-task/scripts/runtimes/__init__.py: runtime factory와 허용 이름 |
| 공통 schema | skills/sg-execute-task/scripts/schemas/verdict.schema.json: 두 runtime이 공유하는 현재 verdict 계약 |
| Codex | skills/sg-execute-task/scripts/runtimes/codex.py: capability preflight, codex exec 명령, JSONL 진단, 최종 verdict 파일 해석 |
| 회귀 테스트 | skills/sg-execute-task/scripts/test_execute.py: 기본 Claude 무변경, runtime 선택, 공통 결과, Codex 계약 테스트 |
| portable skill | skills/sg-plan/SKILL.md: GrillMe 대화 계약 내장, 외부 skill과 필수 delegation 제거 |
| portable skill | skills/sg-decompose-task/SKILL.md: 특정 instruction 파일과 호스트 agent를 전제로 한 표현 제거 |
| portable skill | skills/sg-execute-task/SKILL.md: host와 runtime을 구분하고 두 실행 경로와 안전 고지 설명 |
| portable skill | skills/sg-source-of-truth/SKILL.md: CLAUDE.md 단일 전제 대신 프로젝트 instruction 파일 계약 사용 |
| instruction | AGENTS.md: CLAUDE.md를 공통 개발 지침으로 읽게 하는 짧은 Codex 진입점 |
| hook | hooks/hooks.json, hooks/test_hooks.py: Claude/Codex payload의 안전·차단 fixture 검증 |
| 패키징 | .codex-plugin/plugin.json 생성, .claude-plugin/plugin.json과 버전 1.0.0 정렬, .claude-plugin/marketplace.json 설명 중립화 |
| 문서와 CI | README.md, CLAUDE.md, .github/workflows/test.yml: 두 설치·실행 여정과 전체 테스트 명령 반영 |

Claude 경계 추출 단계에서는 동작 변경과 Codex 기능을 함께 넣지 않는다. 먼저 기존 Claude 테스트를 통과시킨 뒤 CodexRuntime을 추가한다.

CodexRuntime은 section 5.2의 명령 계약을 사용한다. JSONL stdout은 진단과 liveness에 사용하고, 최종 verdict는 --output-schema와 --output-last-message가 기록한 파일에서 읽는다. verdict 파일이 없거나 schema와 맞지 않거나 process가 비정상 종료하면 성공으로 추정하지 않는다.

### 8.4 검증 명령과 완료 판정

구현 중 빠른 검사는 관련 테스트만 실행하되, 각 단계 끝에는 다음 전체 검사를 실행한다.

    python3 -m pytest skills/sg-execute-task/scripts/test_execute.py hooks/test_hooks.py -q
    python3 -m json.tool hooks/hooks.json >/dev/null
    python3 -m json.tool .claude-plugin/plugin.json >/dev/null
    python3 -m json.tool .codex-plugin/plugin.json >/dev/null
    python3 skills/sg-execute-task/scripts/execute.py --help

--help에는 --runtime과 claude/codex 선택지가 보여야 한다. runtime을 생략한 테스트는 ClaudeRuntime을 선택해야 하며, 경계 추출 직후 기존 168개 테스트가 모두 통과해야 한다.

최종 완료 조건은 section 6의 계약 테스트 행렬 전체 통과, 두 manifest의 동일 버전, 공통 skill의 필수 Claude/GrillMe 의존성 제거다.

실제 Claude/Codex smoke test는 unit test 이후 별도의 임시 git 저장소에서 각각 작은 step 하나로 수행한다. 이 저장소 자체에서 executor smoke test를 실행하지 않는다. 실제 agent 실행은 비용과 파일 변경을 수반하므로 사용자 승인 후 수행하고, 인증이 없으면 blocked로 보고 unit/fixture 검증과 혼동하지 않는다.

### 8.5 확정 사항과 중단 조건

확정 사항은 runtime 이름, 기본 Claude, 명시적 선택, 기존 결과 호환성, GrillMe 계약 내장, 공통 hook, 이중 manifest다. 구현자는 이를 다시 설계하지 않는다.

함수의 세부 이름, runtime 내부 parser helper, fixture 배치는 기존 스타일과 테스트 용이성 범위에서 조정할 수 있다. 새 추상화나 옵션은 이 문서의 완료 기준에 직접 필요할 때만 추가한다.

다음 상황에서는 추측해서 범위를 넓히지 말고 작업을 멈춰 기록한다.

- 설치된 공식 CLI가 문서의 필수 옵션을 제공하지 않는다.
- 기존 output JSON 소비자가 additive runtime 필드도 거부한다.
- 두 호스트의 hook payload가 fixture와 달라 하나의 command로 안전하게 처리할 수 없다.
- 기준 테스트가 구현 전부터 실패하며 원인을 분리할 수 없다.

구현 세션에서 commit, push, plugin 설치 또는 실제 child agent 실행은 사용자가 명시적으로 요청한 범위에서만 수행한다.
