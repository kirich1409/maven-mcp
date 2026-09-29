# Plan: maven-mcp 1.0 — локальный запуск из агентов

Slug: maven-mcp-1-0
Status: ready
Created: 2026-09-29

## Goal

Пользователь Claude Code, Grok Build, Codex, Cursor, Claude Desktop, Gemini CLI или Kimi запускает maven-mcp без клона монорепо: плагин — как сейчас из marketplace, остальные клиенты — одной командой `uvx maven-mcp` (или `maven-mcp` после `uv tool install`). Тег релиза — `maven-mcp--v1.0.0`. Сервер остаётся однофайловым Python на стандартной библиотеке.

## Scope

- In:
  - Устанавливаемый entry point `maven-mcp` → существующий `main()` в `plugin/server/server.py`. Runtime-зависимостей нет.
  - Четвёртая точка версии в `pyproject.toml`, проверка в `scripts/validate.sh --check-tag`.
  - README: матрица клиентов, исправление команды Claude Code, `uv` вместо абсолютного пути в монорепо, HTTP для клиентов без локального stdio.
  - Smoke: MCP `initialize` через установленную команду, без checkout.
  - Windows: `gradlew.bat` раньше POSIX `gradlew`, `OSError`/`WinError` не маскируется под успех (#457).
  - В README явно перечислены ограничения, которые 1.0 не закрывает.
  - Релизный коммит `1.0.0` и чеклист тега. Push тега и публикация на PyPI — только после подтверждения.
- Out:
  - Вырезать плагин из `krozov-ai-tools` и переносить закрытую историю issues. Marketplace entry там пока остаётся.
  - npm-обёртка и Homebrew как канал поставки.
  - MCPB-бандл. `scripts/pack-mcpb.sh` завязан на youtube-transcript; для Claude Code и Grok канал — marketplace.
  - `outputSchema` у шести content-only tools, fallback `CHANGELOG.md` в `get_dependency_changes`, резолв репозиториев #318–#320.
  - Починка upstream Grok #572 (`args` в `hooks.json`). У этого плагина поля `args` нет.
  - Жёсткий запрет правок в хуках. Deny остаётся advisory и fail-open.

## Context / sources of truth

- Исследование: сессия `01a0ec7e`, отчёт deep-research (Partial). Поставка сейчас `0.27.2`.
- Решения этой сессии: сервер остаётся Python; для не-plugin клиентов — `uv`/`uvx`, не npm и не brew. Локально Python 3.9+ не гарантирован (Claude Code, Codex, Grok его не ставят). В облачной VM Claude Code Python 3.10–3.13 и `uv` уже есть.
- Версии: `plugins/maven-mcp/plugin/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `SERVER_VERSION` в `server.py` (`USER_AGENT` от него). Гейт: `bash scripts/validate.sh --check-tag maven-mcp--vX.Y.Z`. Голый тег `v1.0.0` релиз не запускает (`release.yml` на `*--v*`, `legacy-tag-guard.yml` валит `v*`). Чеклист: `docs/PLUGIN-STANDARDS.md` §10.
- Клиенты сейчас: `.mcp.json` — `python3` + `${CLAUDE_PLUGIN_ROOT}/server/server.py`. README для остальных указывает абсолютный путь внутрь монорепо. Команда `claude plugin add` в README не существует; нужны `claude plugin marketplace add` / `claude plugin install`.
- `main()` уже есть (`server.py`, transport из `MAVEN_MCP_TRANSPORT`).
- `pyproject.toml` плагина — только tool-конфиг. Комментарий INP001 запрещает пакетную раскладку для ruff/unittest, не запрещает console script.
- #457: `_find_gradle_wrapper` пробует `gradlew` раньше `gradlew.bat`; `_run_gradle_command` ловит только `TimeoutExpired`. PR #458 — открытый draft; configuration-cache и Gradle 9 из того PR в 1.0 не входят, пока нет отдельного repro.
- Хуки: без `jq` молчат; без `timeout`/`gtimeout` pre-edit выходит 0. На macOS нужен `gtimeout` (coreutils).

## Approach

Плагин для Claude Code и Grok не меняет способ запуска: marketplace по-прежнему кладёт каталог плагина и стартует `python3` на bundled `server.py`. Рядом появляется installable-проект с build-backend только на этапе сборки колеса.

Entry point вызывает уже существующий `server.main`. Отдельный рантайм и второй сервер не заводятся. `uvx maven-mcp` сам скачивает Python ≥3.9, поэтому пользователю ставят `uv`, не интерпретатор. В облаке Claude Code `uv` уже есть.

Build-backend: **hatchling**, только `build-system.requires`. В runtime-зависимости сервера он не попадает. Если имя `maven-mcp` занято на PyPI, пакет называется так, как свободно, а команда остаётся `maven-mcp`; имя фиксируется в README в том же слайсе.

Версия в `pyproject.toml` — четвёртая проверяемая точка, не генерируется молча из `plugin.json`.

Windows-фикс — отдельный багфикс с красным тестом до правки. Не смешивать с упаковкой.

Релизный bump `0.27.2` → `1.0.0` — последний коммит, когда entry point и README уже есть. MAJOR по `PLUGIN-STANDARDS` §9: ломается способ подключения «только путь в монорепо» для документированных сторонних клиентов (старый путь `python3 server.py` продолжает работать).

## Verification

План покрывает несколько типов. Уровень обязателен на том слайсе, где есть product-код. Docs-only слайс (T3, T6) — без полной пирамиды.

| Level | Required | How | Skip reason |
|-------|----------|-----|-------------|
| L0 | yes на код | `python3 -m unittest discover -s plugins/maven-mcp/tests` из корня монорепо; для упаковки — `uv build` / `uv tool install --from` этого проекта и процесс стартует | |
| L1a | yes на код | ruff + mypy так, как задано в `plugins/maven-mcp/AGENTS.md`; `bash scripts/validate.sh` и `--check-tag` после четвёртой точки версии | |
| L1b | yes на product-diff | agent `reviewer` на diff слайса | docs-only T3/T6 — skip |
| L2 | yes на T1 и T5 | unittest: entry point резолвится в `main`; Windows-порядок кандидатов и `OSError` — красный тест до фикса | docs — skip |
| L3 | no | | нет UI |
| L4 | no | | нет perf-бюджетa |
| L5 | yes на T1 и T4 | установленная команда отвечает на MCP `initialize` по stdio; один Gradle-вызов на Windows либо зафиксированный unit-репро WinError, если Windows-раннера нет | docs — skip. Живой Windows — если раннера нет, в state записать gap, не писать «готово» для T5 |

DoD после кода: `verify-change`. Любой edit product-кода сбрасывает state в `stale`.

## Test plan

Red-green required: yes, только T5. Остальное — тесты вместе с кодом.

### Smoke

- TC-1: `uv tool install` из каталога плагина ставит команду `maven-mcp`. `command -v maven-mcp` находится без клона в `PATH` сверх install.
- TC-2: stdin `initialize` (JSON-RPC) → ответ с именем сервера и `SERVER_VERSION`, процесс не требует `server.py` по абсолютному пути монорепо.
- TC-3: `MAVEN_MCP_TRANSPORT=http` на `127.0.0.1` со свободным портом отвечает на тот же `initialize` по HTTP и завершается. Порт не 8765, если занят.
- TC-4: Claude/Grok путь не сломан: `.mcp.json` по-прежнему `python3` и `${CLAUDE_PLUGIN_ROOT}/server/server.py`.
- TC-5: `bash scripts/validate.sh --check-tag maven-mcp--v1.0.0` зелёный только когда четыре точки версии равны `1.0.0`. Сдвиг одной точки — красный.

### Feature

- TC-6: README содержит рабочие команды marketplace для Claude Code и `grok plugin install … --trust`. Сниппеты Codex (`[mcp_servers.maven-mcp]` + `uvx`), Cursor, Claude Desktop, Gemini, Kimi используют `uvx`, не путь в монорепо.
- TC-7: README говорит, что веб-ChatGPT локальный stdio не запускает, и даёт HTTP-форму. Облачная VM Claude Code отмечена как «Python и uv уже есть».

### Negative / edge

- TC-8: на Windows (или в unit-тесте с подменённым `os.name` / списком кандидатов) при наличии только `gradlew.bat` выбирается он, а не POSIX `gradlew`. `OSError` с WinError 193 не проглатывается как пустой успех.
- TC-9: отсутствие `jq` и `timeout` описано как fail-open, не как гарантия блокировки правки.
- TC-10: колесо не тянет runtime-зависимости (`Requires-Dist` пуст, кроме явно пустого списка).

## Где лежат задачи

Код и открытые issues — в `kirich1409/maven-mcp`. Из `krozov-ai-tools` перенесены и там больше не резолвятся. Закрытая история (#318 и остальные уже смерженные) осталась в монорепо. Draft PR [#458](https://github.com/kirich1409/krozov-ai-tools/pull/458) не переносился.

Marketplace (`marketplace.json`) и `scripts/validate.sh --check-tag` по-прежнему в монорепо. Этот репозиторий их не содержит.

| Задача | Где | Зачем |
|---|---|---|
| T1–T4, T7 | [#2](https://github.com/kirich1409/maven-mcp/issues/2) | важно для 1.0: `uv` и клиенты. Было #459 |
| T5 | [#1](https://github.com/kirich1409/maven-mcp/issues/1) | Windows `gradlew.bat`. Было #457 |
| T6 | этот план + ссылки на #3–#5 | текст «не в 1.0», без нового issue |
| schema | [#3](https://github.com/kirich1409/maven-mcp/issues/3) | на будущее. Было #460 |
| CHANGELOG.md fallback | [#4](https://github.com/kirich1409/maven-mcp/issues/4) | на будущее. Было #461 |
| хвосты резолва | [#5](https://github.com/kirich1409/maven-mcp/issues/5) | на будущее. Было #462 |

## Tasks

- [ ] T1: Installable entry point. Трекер: #2. В `pyproject.toml` — `[project]` (`requires-python >=3.9`, без runtime-deps) и script `maven-mcp` → `main`. Проверить имя на PyPI до публикации; команду не переименовывать. Раскладка остаётся одним `server.py` (unittest discover не ломать). → verify: TC-1, TC-2, TC-3, TC-4, TC-10, L1a, L1b
- [ ] T2: Четвёртая версия. `pyproject.toml` `version` равен `plugin.json` и `SERVER_VERSION`. Проверка в этом репозитории (монорепный `scripts/validate.sh --check-tag` сюда не скопирован; `marketplace.json` всё ещё в krozov-ai-tools). → verify: TC-5, L1a
- [ ] T3: README «Use with any MCP client» и Installation. Убрать `claude plugin add`. Матрица: Claude Code marketplace, Grok `--trust`, Codex/Cursor/Claude Desktop/Gemini/Kimi через `uvx maven-mcp`, веб-ChatGPT только HTTP, облако Claude Code без доустановки Python. Предусловия хуков: `jq`, на macOS `gtimeout`. → verify: TC-6, TC-7, TC-9
- [ ] T4: Smoke установленного stdio в CI или в скрипте рядом с существующими `scripts/smoke-*.sh`: временный `uv tool install`, `initialize`, удаление tool. Не ходить в сеть Maven. → verify: TC-2, L5
- [ ] T5: #1. Сначала красный тест на порядок `gradlew.bat` и на `OSError`. Затем минимальная правка `_find_gradle_wrapper` и `except`. Configuration-cache / Gradle 9 из draft PR #458 не брать без отдельного repro. → verify: TC-8 red→green, L1b
- [ ] T6: В README или `CLAUDE.md` список «не в 1.0» со ссылками на #3, #4, #5: шесть tools без `outputSchema`, нет fallback на `CHANGELOG.md`, residuals резолва, нет MCPB, нет npm/brew. → verify: ревью текста, код не меняется
- [ ] T7: Релизный коммит `1.0.0`: версии в `plugin.json`, `SERVER_VERSION` и `pyproject.toml` совпадают. Тег и PyPI не пушить без явного подтверждения. Marketplace-тег `maven-mcp--v*` и `validate.sh` всё ещё живут в krozov-ai-tools — их надо либо перенести, либо обновить отдельно. → verify: TC-5 на релизном коммите

Порядок: T1 → T2 → T4. T3 после T1 (нужно финальное имя пакета). T5 параллельно с T1, отдельным коммитом. T6 в любой момент. T7 последним.

## Risks

- Имя `maven-mcp` на PyPI может быть занято. Команда и имя дистрибутива тогда расходятся; зафиксировать в T1 до написания README.
- hatchling — новая build-зависимость. В runtime её нет. Откат: убрать `[project]` и вернуть только tool-конфиг.
- `uv` у пользователя может не быть. Это одна установка, не зависимость сервера. Плагинный путь Claude/Grok `uv` не требует, ему нужен `python3` на машине пользователя; в облачной VM Claude он есть, локально — нет. Этот разрыв 1.0 не закрывает для marketplace-установки.
- Windows L5 без раннера останется unit-репро. Не называть T5 проверенным на живой Windows.
- Четыре копии версии разъедутся, если бампить руками. T2 существует именно поэтому.

## Open questions / decisions

- Q: npm или brew вместо Python? → decision: нет. Сервер Python, поставка для не-plugin клиентов — `uv`/`uvx`. Brew и npm в 1.0 не делаем.
- Q: выносить код в `kirich1409/maven-mcp`? → decision: код залит, открытые issues перенесены (2026-09-29). Релизный marketplace и `validate.sh` пока в монорепо.
- Q: MCPB как у youtube-transcript? → decision: нет в 1.0. Упаковщик захардкожен на другой плагин. Отдельным issue не заводим.
- Q: закрывать #318–#320, schema и changelog fallback до тега? → decision: нет. Хвосты — #3, #4, #5. T6 только ссылается на них.
- Q: Windows wrapper в 1.0? → decision: да, #1. Порядок `gradlew.bat` и `OSError`. Configuration-cache / Gradle 9 из draft PR krozov-ai-tools#458 не входят.
- Q: новая runtime-зависимость? → decision: нет. hatchling только в `build-system.requires`.
- Q: кто пушит тег и PyPI? → decision: не этот план. Нужно явное подтверждение на push тега и на публикацию.

## Handoff

Implement: main в `https://github.com/kirich1409/maven-mcp`. Issues: #1 Windows, #2 uv/1.0, #3 schema, #4 changelog, #5 repo residuals.

После каждого product-слайса: `code-simplifier`, затем `verify-change`. State: `.grok-report/maven-mcp-1-0-state.md` в монорепо.

T7 push тега и PyPI — confirm, не часть «план готов, можно релизовать молча до конца».
