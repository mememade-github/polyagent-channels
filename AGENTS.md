# AGENTS.md — Polyagent Channels development guide

Governance for any coding agent working in this repository. [CLAUDE.md](CLAUDE.md) imports this file, so Claude Code, Codex and other agents read the same guide.

## Context

- Product: a shared message service for users and agents ([README.md](README.md)); context in [PROJECT.md](PROJECT.md); commands, configuration, protocol and recovery in [REFERENCE.md](REFERENCE.md).
- Layout: Python package in `src/polyagent_channels/`, tests in `tests/`, container files in `.devcontainer/`, installer build in `scripts/build_harness.py`.
- This repository carries no agent rule, skill, hook or mirror directories; this file is the whole guide.
- Test commands and the opt-in real-Redis suite are in [REFERENCE.md § Verification](REFERENCE.md#verification).

## Development

Build a small, dependable shared messaging service for users and agents. Keep changes within the requested scope, use standard language/library idioms, give each component a small clear responsibility, and verify the intended result through meaningful empirical checks. State assumptions and resolve root causes. Avoid speculative abstractions and unrelated cleanup.

Use descriptive, searchable names, explicit failures and clear error boundaries. Keep secrets out of tracked files, logs and messages. Read the README for public use and [REFERENCE.md](REFERENCE.md) for operating constraints. Use isolated synthetic test data; clean only resources owned by the test. Run checks appropriate to the changed behavior and report actual commands, results and limitations. Documentation work alone does not authorize source refactoring.

The supplied guide below retains the original code smells, solution alternatives and every Clean Code category. Use it for review and refactoring; it is not a claim that current source mechanically satisfies every item. C009 literally prohibits classes with **two or more** instance variables (not merely more than two). When an item conflicts with a required library interface or minimal standard idiom, surface the specific conflict and its consequences for resolution within the task's authorized scope; do not assume a blanket waiver or silently redefine the item.

## Licensing and attribution

Preserve upstream attribution when distributing inherited files. The inherited DevContainer files and `.dockerignore` are identified in the README's license section. Include `.devcontainer/LICENSE` and `.dockerignore` explicitly in source distributions and retain the existing notice at `/usr/share/doc/polyagent-devcontainer/LICENSE` in images containing inherited scripts. The product is licensed under the MIT License in [LICENSE](LICENSE), copyright (c) 2026 the polyagent-channels authors. Preserve the separate upstream notice.

## Specific guide conflicts

The following exceptions retain existing Python/library idioms at the named boundaries. They do not waive the guide for other code or declare the remaining code smells resolved. Module names below refer to the product's Python package.

| Rule | Location and reason | Consequence and limit |
| --- | --- | --- |
| C009: two or more instance variables | `auth.CredentialAuth` stores the downstream ASGI application, expected authorization header and body limit. These three values serve one authentication boundary; a class is a standard ASGI middleware idiom, though ASGI does not require this storage layout. | Keep these three fields together. Splitting them into single-field wrappers would add indirection without separating a responsibility. This exception is specific to this middleware, not a change to C009's threshold. |
| C005, C010: wrap primitives and collections | `persistence.publish`/`receive` exchange strings, numbers, dictionaries and lists with Redis, JSON and FastMCP; `exchange.new_state`/`validate_state` use a JSON state document; `cli.read_shown_rows` and the functions it calls page through the service's JSON message pages and cursor strings. | Preserve the wire and stored shapes and their boundary validation. Do not introduce wrappers that only unwrap for serialization. This does not excuse missing validation or unrelated groups of domain data. |
| C003, C004: one indentation level, no `else` | `setup.atomic_write` nests file ownership inside `try`/`finally`; `exchange.acquire_lock` and `setup.directory_lock` nest the inode re-check inside descriptor cleanup; `server.lifespan` nests task lifetime and cancellation; `auth.load_key` uses `try`/`except`/`else` to write only a newly created key. | Keep resource acquisition, cleanup and exception scope visible together. This covers these lifetime constructs and successful-creation branch, not arbitrary nested business decisions. |
| C006, C011: one dot, no properties | `client_config.service_url` reads `urlsplit` properties; `cli.DirectConnect` reads the status of websockets' `InvalidStatus` response; `server.messages` and `subscribe` use Starlette request/WebSocket attributes; `setup.canonical_path` uses chained `Path` transformations. | Use the libraries' public interfaces directly. Wrapper getters and temporary variables added solely to count dots would obscure those interfaces; this does not permit exposing product internals through new getters. |
| S010: classes with little behavior | `client_config.ConfigError` subclasses carry distinct diagnostic codes; `server.RequestTooLarge`, `exchange.ExchangeMismatch`, `exchange.ExchangeAlreadyOpen` and `stdio_tools.InvalidParams` distinguish catchable failures. | Retain named exception types for error routing. They are not placeholder domain classes and must not accumulate unrelated state. |
| Clean Code: no flag arguments | `cli.report_page`, `read_shown_rows`, `collect_shown_rows` and `shown_by_read` take `default_read`. A default read and a `--cursor` history read share one paging and reporting path and differ only in which rows they show and whether they record delivery. Separate paths would duplicate the paging limits, and selecting the filter by passing a function conflicts with "more methods rather than passing code". | Keep the two read modes on one path so paging limits and the result shape cannot drift apart. This covers `default_read` only, not new flags. |
| S010: classes with little behavior | `cli.DirectConnect` subclasses websockets' `connect` only to override `process_redirect`, the hook through which the library decides whether to follow a redirect. | Keep it stateless and limited to refusing redirects, so the shared key never reaches another address. |
| Clean Code: no flag arguments | `cli.run_receiver(options, join_default, redeliver)` takes the start stages that the client hook interface (`SessionStart.source`: startup, resume or compact) and `watch --redeliver` select. Both stages must run inside the single receiver slot and before its retry loop. | Separate entry points would repeat that slot and loop order. Limited to these two values; a new start condition needs its own review. |
| Clean Code: dependency injection, no logical dependency | `persistence.configure_storage()` sets the module's Redis client and storage limits with `global`, once, from `server.create_app()`. FastMCP's `mcp.tool()` registers module functions and `StreamableHTTPSessionManager.run()` runs once per process, so the service is a single module-level application and passing a storage object would not make it reusable. | `create_app()` is the only caller and this is the package's only `global`. A second `create_app()` in one process is unsupported. Tests replace `persistence.redis` directly. |
| Tests: one assert per test | A test may compare the outcomes of one behavior as one tuple, `assert (a, b) == (x, y)`; pytest's assertion rewriting reports which element differs. `tests/test_identity_conversations.py::test_shared_auth_never_looks_up_redis_identity` also checks the response and that Redis identity lookup was never awaited in the same request. | A tuple holds only the outcomes of one behavior, including the setup checks it depends on; independently triggered behaviors get their own tests. Do not chain conditions with `and` in one assert, and do not split a scenario solely to satisfy an assertion count. |

## Supplied code guide (original wording)

[코드 스멜 - 문제 원인]
S001. 중복된 코드 기능 :: 데이터 코드가 중복되어 작성
S002. 너무 긴 메서드 :: 메서드가 너무 김
S003. 방대한 클래스 :: 한 클래스가 너무 많은 속성과 메서드를 가짐
S004. 과다한 매개변수 :: 메서드의 매개변수가 너무 많음
S005. 두 가지 이유로 수정되는 클래스 :: 클래스는 한가지 종류의 책임만을 가져야 하기에 2가지 이상의 이유로 수정되면 안됨
S006. 여러 클래스를 동시에 수정 :: 특정 클래스를 수정할 때마다 다른 클래스들에서 변경을 수행해야 하는 경우
S007. 데이터 뭉치 :: 합쳐서 다뤄야 할 데이터가 한 클래스에 있지 않음
S008. 기본 타입 집착 :: 클래스를 만들지 않고 int와 같은 기본형 데이터 타입만 사용하는 경우
S009. 스위치 문 :: switch문이나 if 문으로 동작을 나누는 경우
S010. 게으른 클래스 :: 클래스가 하는 역할이 별로 없는 경우
S011. 의심스러운 일반화 :: 언젠가 확장 하겠지, 하며 일반화
S012. 클래스 인터페이스 불일치 :: 클래스 인터페이스가 적절하지 않음
S013. 불완전한 라이브러리 클래스 :: 기존 라이브러리 클래스를 사용하기 어려움
S014. 평행상속 :: 하위 클래스를 만들면 클래스 계층의 다른 곳에도 하위 클래스를 만들어야 함
S015. 주석 :: 코드의 모자란 부분을 설명하기 위한 자세한 주석이 존재

[체크리스트 - 해결 대안]
C001. 한 번에 하나의 기능 추가 또는 하나의 리팩터링만 Commit 합니다
C002. 코드 커버리지를 확인합니다
C003. 한 메서드에 오직 한 단계의 들여쓰기 합니다.
C004. else 예약어를 쓰지 않습니다.
C005. 모든 원시 값과 문자열을 포장합니다.
C006. 한 줄에 점을 하나만 찍습니다.
C007. 줄여서 쓰지 않습니다.
C008. 모든 엔티티를 작게 유지합니다.
C009. 2개 이상의 인스턴스 변수를 가진 클래스를 쓰지 않습니다.
C010. 일급 콜렉션을 씁니다.
C011. 게터/세터/프로퍼티를 쓰지 않습니다.

[클린코드]
일반적인 규칙
- 표준 컨벤션을 따르자.
- convention: 읽고, 관리하기 쉬운 코드를 작성하기 위한 코딩 스타일 규약
- 간단하게 만들어라. 복잡한 것을 최대한 줄여라.
- 찾았을 때보다 더 깨끗하게 만들고 떠나라.
- 항상 문제의 근본 원인을 찾아라.
디자인 규칙
- 데이터를 high-level로 유지하라.
- if/else 또는 switch/case 보다 다형성을 선호하라.
- multi-threading 코드를 분리하라.
- 과도한 구성을 방지하라.
- 의존성 주입을 사용하라.
- 클래스는 직접적인 의존성만 알아야 한다.
이해도 높이는 팁
- 일관되게 하라. 당신이 어떤 특정한 방법을 사용한다면, 모든 것들을 같은 방법으로 만들어라.
- 설명 변수를 사용하라.
- 경계 조건을 캡슐화 하라.
- 기본 형식보다 전용 객체를 선호하라.
- 논리적 종속성을 피하라. 같은 클래스의 다른 것에 의존해야 올바르게 작동하는 메서드를 작성하지 마라.
- 부정적인 조건을 피하라.
네이밍 규칙
- 알기 쉬운 이름을 사용하라.
- 의미 있게 구분하라.
- 발음하기 쉬운 이름을 사용하라.
- 검색하기 쉬운 이름을 사용하라.
- 매직 넘버를 상수로 바꾸어라.
- magic number: 코드 안에 적힌 구체적인 수치 값
- 인코딩을 피하라. 접두어나 타입 정보를 더하지 마라.
함수 규칙
- 작게 만들어라.
- 하나의 기능만 수행하게 해라.
- 서술적인 이름을 사용하라.
- 더 적은 인수를 사용하라.
- 사이드 이펙트가 일어나지 않게 하라.
- side effect: 원래의 목적과 다른 외부 효과
- 플래그 인수를 사용하지 마라.
주석 규칙
- 항상 코드로 의도를 표현하려고 애써라.
- 같은 내용을 중복하지 마라.
- 쓸데없는 잡음을 추가하지 마라.
- 닫는 괄호에 주석을 추가하지 마라.
- 주석으로 코드를 처리하지 마라. 그냥 지워라.
- 의도를 설명하는 의미로 사용하라.
- 의미를 명료하게 밝히는 의미로 사용하라.
- 결과를 경고하는 의미로 사용하라.
소스 코드 구조
- 개념은 빈 행으로 분리하라.
- 연관된 코드는 세로로 가깝게 유지하라.
- 변수를 그들의 사용처에 가깝게 선언하라.
- 종속된 메서드를 가깝게 유지하라.
- 유사한 기능을 가깝게 유지하라.
- 함수를 아래 방향으로 위치하라.
- 길이를 짧게 유지하라.
- 가로 정렬을 사용하지 마라.
- 연관된 것들을 연관시키고 상대적으로 비연관성인 것들은 제거하기 위해 공백을 사용하라.
- 들여쓰기를 깨지 마라.
객체와 자료구조
- 내부의 구조를 숨겨라.
- 자료구조를 선호하라.
- 잡종 구조를 만들지 마라.
- 작게 만들어라.
- 한 가지 기능을 하게 만들어라.
- 인스턴스 변수의 갯수를 작게 하라.
- 베이스 클래스가 자신이 조작하는 객체의 속사정을 모르게 하라.
- 어떤 동작을 선택하기 위해, 메서드에 일부 코드를 전달하는 것 보다 많은 메서드를 만드는 것이 낫다.
- 정적 메서드보다 동적 메서드를 선호하라.
테스트
- 하나의 테스트 당 assert 하나
- 읽기 쉽게.
- 빠르게.
- 독립적으로.
- 반복 가능하게.
코드 스멜
- 강직성. 코드를 변경시키기 어려움. 작은 변경이 일련의 변화를 일으킬 수 있음.
- 취약성. 작은 변화 만으로도 파괴가 될 수 있음.
- 부동성. 높은 위험 때문에 코드의 일부분을 다른 프로젝트에서 재사용 할 수 없음.
- 불필요한 복잡성.
- 불필요한 반복.
- 불투명함. 이해하기 어려운 코드. - 이는 코드 지침
