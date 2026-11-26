---
trigger: always_on
---
# Polyagent Channels development guide

Governance for any coding agent working in this repository. Claude Code loads this project rule. Other clients can find it through [PROJECT.md](../../../PROJECT.md).

## Core principles

- **Used knowledge.** Documents, settings and notes hold what a reader acts on. History is not used knowledge.
- **Evidence from behavior.** A claim holds once the real behavior was run and observed.
- **No proof by form.** Counting items, checklists, gates and repeated audits do not show compliance.
- **Knowledge SoT/DAG.** Each fact has one owner; every other place links to it or is derived from it, and derivation runs one way. The aim is knowledge free of duplication and contradiction in meaning, which rewording sentences does not give.
- **Occam's razor.** Use the fewest mechanisms that meet the need.
- **Concurrency.** State that several sessions or processes change at once stays correct; show it by running them together.
- **Concurrent throughput.** After a change the service still serves many sessions at once.

## Context

- Code and Compose definitions are the origin of behavior facts. The dependency order is executable definitions → shared run-time text → consuming run-time surfaces → operator documentation → entry-point documents. A summary never supplies evidence for its origin; links are navigation, not derivation.
- `scripts/render_docs.py` derives the marked `reference` region in REFERENCE.md (configuration and command defaults, client procedures, agent policy, reception, protocol, storage limits and upgrade commands) and the `name` region in UPGRADING.md from package definitions and Compose defaults. `scripts/REFERENCE.md.in` holds the operator prose and substitutions for the first region; edit it instead of the rendered body. Regenerate from the repository root with `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python scripts/render_docs.py` in the test environment from [Verification](../../../REFERENCE.md#verification). `tests/test_documents.py` compares the committed documents with that output. Other documents link to these readable facts; source links beside them supply provenance.
- The renderer also derives the [Shared paths table](../../../DEVCONTAINER-BASE.md#shared-paths) from `SHARED_PATHS` in [scripts/update_devcontainer_base.py](../../../scripts/update_devcontainer_base.py). Edit the mapping to change the row guidance.
- Run-time owners: `harness.py` owns the executable first-connection and client-update procedure; `stdio_tools.py` owns agent instructions and tool descriptions; `cli.py` owns command option help and diagnostic actions; `setup.py` owns installation's client-specific next step and removal notice; `server.py` owns the HTTP, WebSocket and MCP protocol reference. Shared validation limits and defaults come from the code that enforces them. CLI notices and hints consume the policy text in `stdio_tools.py`; client surfaces consume the shared root, restart and transport guidance in `client_config.py`, and `/help` directs the operator to installation's result. Keep only what the reader needs at that moment; required repetition is imported or generated from its owner.
- [REFERENCE.md](../../../REFERENCE.md) owns operator configuration, client lifecycle details, topic membership, reception mechanics, storage, recovery and verification. Within it, link to the owning section rather than repeat a fact. It links to the run-time owners for installation, agent response policy, tool schemas, diagnostics and protocol details.
- [.devcontainer/.env.example](../../../.devcontainer/.env.example) is an override form, not another defaults reference; deployed defaults belong to [compose.channels.yml](../../../.devcontainer/compose.channels.yml), and explanations belong to REFERENCE.md. Python fallback defaults apply only outside Compose; do not present them as independent deployment defaults.
- [UPGRADING.md](../../../UPGRADING.md) owns only release-specific actions beyond REFERENCE.md's upgrade procedure. [PROJECT.md](../../../PROJECT.md) owns known limitations and links to the version in [pyproject.toml](../../../pyproject.toml) and the operational owners. [README.md](../../../README.md) owns product scope, prerequisites, discovery and attribution; it links to those owners for procedures and limits.
- This guide owns development rules: Core principles own the general knowledge and verification rules, Development owns scope and reporting, and the supplied code guide owns code rules. Repeated guide items link to their owning item; Specific guide conflicts owns only the named exceptions. Apply [Knowledge SoT/DAG](#core-principles) to every carrier, including this guide.
- Layout: Python package in `src/polyagent_channels/`, tests in `tests/`, container files in `.devcontainer/`, installer build in `scripts/build_harness.py`.
- The repository carries upstream agent rules, skills, hooks and mirrors. This file owns the product-specific development guide.
- For the container context in AGENTS.md and CLAUDE.md, see the [DevContainer settings](../../../DEVCONTAINER-BASE.md#devcontainer-settings) for entrypoint-driven setup, `core.filemode` repair and mounts, [Compose layering](../../../DEVCONTAINER-BASE.md#compose-layering) for DNS, and [Base environment inputs](../../../DEVCONTAINER-BASE.md#base-environment-inputs) for optional nvm project Node, system Node and timezone.
- In the upstream DevContainer patterns, the Windows/WSL2 path translation applies when the Docker daemon sees the workspace at a different path. Follow the [workspace bind guidance](../../../DEVCONTAINER-BASE.md#base-environment-inputs) for Channels. The GUI table describes the upstream workflow inside the container.
- The advisory gate summary in AGENTS.md and CLAUDE.md describes commit and push gates. Pre-commit accepts markers through 86,400 seconds inclusive, and pre-push recognizes a one-use force-push exception marker. See the Claude [pre-commit](../../../.claude/hooks/pre-commit-gate.sh) and [pre-push](../../../.claude/hooks/pre-push-gate.sh) gates and their Codex [pre-commit](../../../.codex/hooks/pre-commit-gate.sh) and [pre-push](../../../.codex/hooks/pre-push-gate.sh) counterparts.
- Test commands and the opt-in real-Redis suite are in [REFERENCE.md § Verification](../../../REFERENCE.md#verification).

## Development

Keep changes within the requested scope and avoid unrelated cleanup. State assumptions. Apply the [Core principles](#core-principles) when verifying results and the [supplied code guide](#supplied-code-guide) when implementing or reviewing code.

Use explicit failures and clear error boundaries. Keep secrets out of tracked files, logs and messages. Read the README for public use and [REFERENCE.md](../../../REFERENCE.md) for operating constraints. Use isolated synthetic test data; clean only resources owned by the test. Run checks appropriate to the changed behavior and report actual commands, results and limitations. Documentation work alone does not authorize source refactoring.

The supplied code guide is not a claim that current source mechanically satisfies every item. When an item conflicts with a required library interface or minimal standard idiom, surface the specific conflict and its consequences for resolution within the task's authorized scope; do not assume a blanket waiver or silently redefine the item.

## Licensing and attribution

Preserve the notices and distribution locations specified in [README.md § License & attribution](../../../README.md#license--attribution).

## Specific guide conflicts

The following exceptions retain existing Python/library idioms at the named boundaries. They do not waive the guide for other code or declare the remaining code smells resolved. Module names below refer to the product's Python package.

| Rule | Location and reason | Consequence and limit |
| --- | --- | --- |
| [S004](#s004) | `persistence.publish` and `persistence.lease_session` take one keyword per wire field: FastMCP derives the `publish` tool schema from its signature, and HTTP routes pass validated JSON objects as keywords. | An optional wire field adds a parameter. This covers wire fields only, never behavior flags. |
| [C009](#c009) | `auth.CredentialAuth` stores the downstream ASGI application, expected authorization header and body limit. These three values serve one authentication boundary; a class is a standard ASGI middleware idiom, though ASGI does not require this storage layout. | Keep these three fields together. Splitting them into single-field wrappers would add indirection without separating a responsibility. This exception is specific to this middleware. |
| [C005](#c005), [C010](#c010) | `persistence.publish`/`receive` exchange strings, numbers, dictionaries and lists with Redis, JSON and FastMCP; `exchange.new_state`/`validate_state` use a JSON state document; the announced `topics` list and `membership` joined-topics file are JSON at their boundary and validated there; `cli.read_shown_rows` and the functions it calls page through the service's JSON message pages and cursor strings. | Preserve the wire and stored shapes and their boundary validation. Do not introduce wrappers that only unwrap for serialization. This does not excuse missing validation or unrelated groups of domain data. |
| [C005](#c005) | Wire-format limits and defaults in `identity` and `persistence`, the client root in `client_config`, and shared run-time text in `stdio_tools`, `cli`, `setup` and `client_config` feed validation, library defaults and rendered output directly. | Keep these boundary constants as strings or numbers; wrapping them would only require unwrapping for regex, argument parsing or formatting. Limited to these shared boundary definitions. |
| [C003](#c003), [C005](#c005), [C006](#c006), [C010](#c010), [behavior selection](#behavior-selection) | `scripts/render_docs.py` consumes module dictionaries, argparse namespaces, AST nodes and Path interfaces to render text. `re.sub` requires a replacement callback for named substitutions; help capture and validation keep exception handling inside temporary context overrides. | Keep these library interfaces and serialized values at the documentation boundary; wrapper objects or split cleanup scopes would obscure the derivation. This covers the renderer only, not product domain data or behavior selection. |
| [C003](#c003), [C005](#c005), [C006](#c006), [C010](#c010) | `scripts/update_devcontainer_base.py` reads NUL-delimited Git tree records and retains mode and blob strings until writing each file; `tests/test_devcontainer_base.py` compares the JSON lock with dictionaries keyed by Git path strings. The importer keeps per-record validation with its loops and uses standard Path and subprocess interfaces directly. | Keep these serialized values, collections and direct library calls at the Git, JSON and filesystem boundaries. Wrappers would add conversions at those same boundaries; splitting per-record checks would separate a record from its failure. Limited to the base importer and lock test. |
| [C009](#c009), [C011](#c011) | `codex_git.CodexGit` is a frozen record of the worktree, common Git directory, project and client wrapper paths. Its properties derive Git paths, rules and driver identity from those inputs. | Keep these four paths together. The properties compute Git boundary values and expose no mutable state. |
| [C003](#c003), [C005](#c005), [C006](#c006), [C010](#c010) | `codex_git` uses standard subprocess, Path and ZIP interfaces and retains Git's bytes, modes, object IDs, config strings and settings map at those boundaries. Index refresh keeps locking, copying and publication in one cleanup scope. `codex_config` uses the TOML parser's dictionaries, scalars and text and records owned shapes in tuples. | Keep the serialized forms at these boundaries. Wrappers would require conversions back to the same types, and splitting index cleanup would obscure lock ownership. Limited to Git, archive and TOML integration and their shared run-time text. |
| [Flag arguments](#flag-arguments) | `codex_config.add_registration(..., filtered=...)` selects whether a new registration carries Git-filter ownership metadata and whether an owned plain block migrates to that form. Both paths share TOML validation and block replacement; the filter needs the added setting and separator recorded in the file after Git rewrites it. | Keep this choice at the shared registration transformation. Limited to the ownership representation and its migration, not new behavior flags. |
| [C003](#c003), [C004](#c004) | `setup.atomic_write` nests file ownership inside `try`/`finally`; `exchange.acquire_lock` and `setup.directory_lock` nest the inode re-check inside descriptor cleanup; `server.lifespan` nests task lifetime and cancellation; `auth.load_key` uses `try`/`except`/`else` to write only a newly created key. | Keep resource acquisition, cleanup and exception scope visible together. This covers these lifetime constructs and successful-creation branch, not arbitrary nested business decisions. |
| [C006](#c006), [C011](#c011) | `client_config.service_url` reads `urlsplit` properties; `cli.DirectConnect` reads the status of websockets' `InvalidStatus` response; `server.messages` and `subscribe` use Starlette request/WebSocket attributes; `setup.canonical_path` uses chained `Path` transformations. | Use the libraries' public interfaces directly. Wrapper getters and temporary variables added solely to count dots would obscure those interfaces; this does not permit exposing product internals through new getters. |
| [S010](#s010) | `client_config.ConfigError` subclasses carry distinct diagnostic codes; `server.RequestTooLarge`, `exchange.ExchangeMismatch`, `exchange.ExchangeAlreadyOpen`, `exchange.SessionTopicLimit` and `stdio_tools.InvalidParams` distinguish catchable failures. | Retain named exception types for error routing. They are not placeholder domain classes and must not accumulate unrelated state. |
| [Flag arguments](#flag-arguments) | `cli.report_page`, `read_shown_rows`, `collect_shown_rows` and `shown_by_read` take `default_read`. A default read and a `--cursor` history read share one paging and reporting path and differ only in which rows they show and whether they record delivery. Separate paths would duplicate the paging limits, and selecting the filter by passing a function conflicts with [behavior selection](#behavior-selection). | Keep the two read modes on one path so paging limits and the result shape cannot drift apart. This covers `default_read` only, not new flags. |
| [S010](#s010) | `cli.DirectConnect` subclasses websockets' `connect` only to override `process_redirect`, the hook through which the library decides whether to follow a redirect. | Keep it stateless and limited to refusing redirects, so the shared key never reaches another address. |
| [Flag arguments](#flag-arguments) | `cli.run_receiver(options, join_default, redeliver)` takes the start stages that the client hook interface (`SessionStart.source`: startup, resume or compact) and `watch --redeliver` select. Both stages must run inside the single receiver slot and before its retry loop. | Separate entry points would repeat that slot and loop order. Limited to these two values; a new start condition needs its own review. |
| [Dependency injection](#dependency-injection), [logical dependency](#logical-dependency) | `persistence.configure_storage()` sets the module's Redis client and storage limits with `global`, once, from `server.create_app()`. FastMCP's `mcp.tool()` registers module functions and `StreamableHTTPSessionManager.run()` runs once per process, so the service is a single module-level application and passing a storage object would not make it reusable. | `create_app()` is the only caller and this is the package's only `global`. A second `create_app()` in one process is unsupported. Tests replace `persistence.redis` directly. |
| [One assertion](#one-assert) | A test may compare the outcomes of one behavior as one tuple, `assert (a, b) == (x, y)`; pytest's assertion rewriting reports which element differs. `tests/test_identity_conversations.py::test_shared_auth_never_looks_up_redis_identity` also checks the response and that Redis identity lookup was never awaited in the same request. | A tuple holds only the outcomes of one behavior, including the setup checks it depends on; independently triggered behaviors get their own tests. Do not chain conditions with `and` in one assert, and do not split a scenario solely to satisfy an assertion count. |

## Supplied code guide

### Code smells — causes

- <a id="s001"></a>**S001. Duplicated functionality.** Data or code is written more than once.
- <a id="s002"></a>**S002. Long methods.** Apply [C008](#c008) to methods.
- <a id="s003"></a>**S003. Large classes.** Too many attributes and methods; apply [C008](#c008) and [C009](#c009).
- <a id="s004"></a>**S004. Too many parameters.** Keep method parameter lists small.
- <a id="s005"></a>**S005. More than one reason to change.** A class must have one kind of responsibility and therefore one reason to change.
- <a id="s006"></a>**S006. Changes spread across classes.** Changing one class requires changes in several others.
- <a id="s007"></a>**S007. Data clumps.** Data that belongs together is not in one class.
- <a id="s008"></a>**S008. Primitive obsession.** See [C005](#c005).
- <a id="s009"></a>**S009. Switch statements.** For behavior selected with `switch` or `if`, see [Design rules](#design-rules).
- <a id="s010"></a>**S010. Lazy classes.** A class has too little behavior to justify its existence.
- <a id="s011"></a>**S011. Speculative generality.** Generalization assumes that an extension will be needed someday.
- <a id="s012"></a>**S012. Inconsistent class interfaces.** A class has an unsuitable interface.
- <a id="s013"></a>**S013. Incomplete library classes.** An existing library class is difficult to use.
- <a id="s014"></a>**S014. Parallel inheritance.** Adding a subclass requires another subclass elsewhere in the hierarchy.
- <a id="s015"></a>**S015. Explanatory comments.** Detailed comments compensate for shortcomings in the code; see [Comment rules](#comment-rules).

### Checklist — solution alternatives

- <a id="c001"></a>**C001.** Commit only one feature addition or one refactoring at a time.
- <a id="c002"></a>**C002.** Check code coverage; interpret it under [Evidence from behavior and No proof by form](#core-principles).
- <a id="c003"></a>**C003.** Use only one level of indentation per method.
- <a id="c004"></a>**C004.** Do not use the `else` keyword.
- <a id="c005"></a>**C005.** Wrap all primitives, such as `int`, and strings in dedicated objects.
- <a id="c006"></a>**C006.** Use only one dot per line.
- <a id="c007"></a>**C007.** Do not abbreviate names.
- <a id="c008"></a>**C008.** Keep every entity small.
- <a id="c009"></a>**C009.** Do not use classes with **two or more** instance variables.
- <a id="c010"></a>**C010.** Use first-class collections.
- <a id="c011"></a>**C011.** Do not use getters, setters or properties.

### Clean Code

#### General rules

- Follow standard language and library conventions: coding style agreements that make code easy to read and maintain.
- For simplicity and reducing complexity, apply [Occam's razor](#core-principles).
- Leave code cleaner than you found it, within the [Development scope](#development).
- Always find the root cause of a problem.

#### Design rules

- Keep data at a high level of abstraction.
- Prefer polymorphism to `if/else` or `switch/case`; [C004](#c004) governs `else` use.
- Separate multithreading code.
- For avoiding overdesign, apply [S011](#s011) and [Occam's razor](#core-principles).
- <a id="dependency-injection"></a>Use dependency injection.
- Classes should know only their direct dependencies.

#### Improving understanding

- Be consistent: apply a chosen approach consistently wherever it is used.
- Use explanatory variables.
- Encapsulate boundary conditions.
- For dedicated objects instead of primitives, see [C005](#c005).
- <a id="logical-dependency"></a>Avoid logical dependencies: do not write a method that depends on something else in the same class to work correctly.
- Avoid negative conditions.

#### Naming rules

- Use clear names.
- Make meaningful distinctions.
- Use pronounceable names.
- Use searchable names.
- Replace magic numbers (numeric literals embedded in code) with named constants.
- Avoid encodings: do not add prefixes or type information to names.

#### Function rules

- For function size, see [C008](#c008).
- A function should perform one operation.
- For descriptive function names, see [Naming rules](#naming-rules).
- For fewer arguments, see [S004](#s004).
- Avoid side effects: external effects beyond the function's purpose.
- <a id="flag-arguments"></a>Do not use flag arguments.

#### Comment rules

- Express intent in code whenever possible.
- For repeating information, apply [Knowledge SoT/DAG](#core-principles).
- Do not add noise.
- Do not add comments at closing braces.
- Delete commented-out code.
- Use comments to explain intent.
- Use comments to clarify meaning.
- Use comments to warn of consequences.

#### Source code structure

- Separate concepts with blank lines.
- Keep related code vertically close.
- Declare variables close to their use.
- For dependent methods, apply the related-code placement rule above.
- For similar functionality, apply the related-code placement rule above.
- Order functions downward from callers to the functions they call.
- For short code units, see [C008](#c008).
- Do not align code horizontally.
- For whitespace that groups related code and separates unrelated code, apply the placement rules above.
- Preserve indentation.

#### Objects and data structures

- Hide internal structure.
- Prefer data structures.
- Do not mix objects and data structures into hybrids.
- For size, see [C008](#c008).
- For a single responsibility, see [S005](#s005).
- For instance-variable count, see [C009](#c009).
- Base classes should not know the internals of the objects they manipulate.
- <a id="behavior-selection"></a>Prefer more methods to passing code into a method to select its behavior.
- Prefer instance methods to static methods.

#### Tests

- <a id="one-assert"></a>One assertion per test, interpreted under [No proof by form](#core-principles) and the [scoped exception](#specific-guide-conflicts).
- Make tests readable.
- Make tests fast.
- Make tests independent.
- Make tests repeatable.

#### Code smells

- Rigidity: see [S006](#s006) for changes that spread through the code.
- Fragility: a small change can break the code.
- Immobility: reusing part of the code in another project carries a high risk.
- Needless complexity: see [Occam's razor](#core-principles).
- Needless repetition: see [S001](#s001).
- Opacity: code is difficult to understand. These are code guidelines.
