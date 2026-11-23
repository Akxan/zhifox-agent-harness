<!--
Thanks for contributing! Please read CONTRIBUTING.md first. Security fixes: report the problem privately first (SECURITY.md).
谢谢你的贡献！请先读 CONTRIBUTING.md。安全问题请先照 SECURITY.md 私下报告，再提修复。
-->

## What and why / 改了什么、为什么

<!-- What changes for the user, and why. Link the issue: "Fixes #123". / 对用户来说有什么不同、为什么要改；关联的 issue 写 "Fixes #123"。 -->

## How it was tested / 怎么测的

<!-- Which tests you added, what you tried by hand (for example in `uv run python tests/demo_web.py`), and on which systems.
     Say what you could not test; CI covers Ubuntu, macOS and Windows.
     加了哪些测试、手动试了什么（比如在 tests/demo_web.py 里）、在哪几个系统上；没测到的也写上，CI 会在三个系统上跑。 -->

## Checklist / 检查清单

- [ ] Tests added or updated; for a bug, a test that fails without the fix comes first. / 加了或改了测试；修 bug 先写一个没修时会失败的测试。
- [ ] `uv run pytest -q` passes, with no real API, bot or paid service called. / `uv run pytest -q` 通过，没有调用真的 API、机器人或收费服务。
- [ ] `uvx ruff@0.17.6 check harness tests` and `uv run --with mypy==1.19.0 mypy --platform …` (linux, darwin, win32) pass. / ruff 和 mypy（三个系统）通过。
- [ ] If behavior changed: the matching pages in `docs/en/` and `docs/zh-CN/` (and `README.md` / `README.zh-CN.md` if the overview changed) are updated and say the same thing. / 行为变了的话，docs/ 下中英两份对应的页面（概览变了的话还有两份 README）都改了，内容一致。
- [ ] New text in the interface is wrapped in `t()`, with translations in `harness/locales/en.json` and `es.json`. / 界面上新的文字包了 `t()`，并在 en.json、es.json 里加了译文。
- [ ] Considered macOS, Windows and Linux (shortcuts, paths, shell commands, what isn't supported where). / 想过 macOS、Windows、Linux 上各是什么样（快捷键、路径、命令、哪里不支持）。
- [ ] No secrets, `.env` or `data/` in the diff; only made-up keys in tests and screenshots. / 改动里没有密钥、`.env` 和 `data/`；测试和截图里只有编造的密钥。
