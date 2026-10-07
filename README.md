# Naren's Claude Toolkit

<p align="center">
  <img src="assets/banner.svg" alt="Naren's Claude Toolkit: skills, mods and MCP servers for Claude Code" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-7C5CFF"></a>
  <img alt="Plugins" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2FNarenDawar%2Fnarens-claude-toolkit%2Fmain%2F.claude-plugin%2Fmarketplace.json&query=%24.plugins.length&label=plugins&color=7C5CFF">
  <a href="https://github.com/NarenDawar/narens-claude-toolkit/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/NarenDawar/narens-claude-toolkit?style=flat&color=7C5CFF"></a>
</p>

**Naren's toolkit for [Claude Code](https://claude.com/claude-code): skills, mods and MCP servers, each small, focused and tested.** Install skills and mods in one command through the plugin marketplace, or copy a `SKILL.md` folder into your own setup.

## Install

**Plugin marketplace (recommended)**

```text
/plugin marketplace add NarenDawar/narens-claude-toolkit
/plugin install <plugin-name>@narens-claude-toolkit
```

**Manual copy**

Copy any `plugins/<skill-name>/skills/<skill-name>/` folder into `~/.claude/skills/`. Claude picks it up on the next session.

## Moving from narens-claude-skills

This repo used to be `narens-claude-skills`. The marketplace is now `narens-claude-toolkit`, so if you installed a plugin from the old marketplace, move once:

```text
/plugin marketplace remove narens-claude-skills
/plugin marketplace add NarenDawar/narens-claude-toolkit
/plugin install <plugin-name>@narens-claude-toolkit
```

Plugin names, versions and behavior are unchanged. Removing the old marketplace may also remove the plugins you installed from it, so reinstall each one with the new suffix. Old GitHub links keep redirecting.

## What is inside

<!-- CATALOG:START -->
### Skills

| Skill | What it does | Install |
| --- | --- | --- |
| [`comprehension-check`](plugins/comprehension-check/README.md) | Use when the user asks to be quizzed on, or to check their understanding of, code Claude wrote this session (for example 'quiz me on that' or 'do I actually understand this change?'). | `/plugin install comprehension-check@narens-claude-toolkit` |
| [`decision-journal`](plugins/decision-journal/README.md) | Use when the user wants to log a prediction or estimate about a dev or project decision, grade past predictions, or see how calibrated they are (for example 'log a prediction', 'grade my predictions', 'how calibrated am I?'). | `/plugin install decision-journal@narens-claude-toolkit` |
| [`rule-promoter`](plugins/rule-promoter/README.md) | Use when the user wants rules in CLAUDE.md enforced with hooks, says Claude keeps ignoring a CLAUDE.md rule, or asks to turn CLAUDE.md rules into hooks (for example 'promote my CLAUDE.md rules to hooks' or 'make Claude stop editing migrations'). | `/plugin install rule-promoter@narens-claude-toolkit` |
| [`schedule-doctor`](plugins/schedule-doctor/README.md) | Use when the user is about to schedule a Claude task, or asks why a scheduled task, /loop, cron job or routine did not run, ran late, or stopped partway (for example 'will this scheduled task actually run?', 'why didn't my 7am task fire?', 'check this before I schedule it'). | `/plugin install schedule-doctor@narens-claude-toolkit` |
| [`subagent-tax-auditor`](plugins/subagent-tax-auditor/README.md) | Use when the user asks why their Claude Code quota or usage limit runs out so fast, which subagents cost the most, or wants their subagents made cheaper (for example 'audit my subagents', 'which agent is eating my quota', 'make my agents cheaper'). | `/plugin install subagent-tax-auditor@narens-claude-toolkit` |

### Mods

| Mod | What it does | Install |
| --- | --- | --- |
| [`local-answer-router`](plugins/local-answer-router/README.md) | Answers trivial questions (what branch, what changed, did the last test pass) from git and the last test run on your machine, with no model tokens. | `/plugin install local-answer-router@narens-claude-toolkit` |
| [`subagent-meter`](plugins/subagent-meter/README.md) | A live status line showing how much of your session's tokens go to subagents, and which agent type uses the most. | `/plugin install subagent-meter@narens-claude-toolkit` |

### MCP servers

| Server | What it does | Folder |
| --- | --- | --- |
| [`mcp-fixer`](servers/mcp-fixer/README.md) | Scores an MCP server's tool definitions with deterministic lint rules, so you can see what makes agents pick the wrong tool or waste tokens. | [`servers/mcp-fixer`](servers/mcp-fixer) |
<!-- CATALOG:END -->

## Why this toolkit

Most Claude Code collections are generic prompt dumps. These are small, focused, and opinionated: each one solves a specific problem, behaves predictably, and is tested before it ships. Every skill and mod is its own plugin, so you install only what you need.

## Build your own

The repo includes a skill template and a validator. See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/conventions.md](docs/conventions.md) for how skills are structured (`SKILL.md` frontmatter, trigger-first descriptions, per-skill READMEs).

## License

[MIT](LICENSE)

---

Made by [Naren](https://github.com/NarenDawar). If something here saved you time, please star the repo.
