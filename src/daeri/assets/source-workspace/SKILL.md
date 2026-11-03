---
name: daeri-source-workspace
description: Develop source changes in the current DAERI task's isolated Git branches using the configured project repositories.
---

Use this skill for source development after the orchestrator's approved plan includes the repository changes.
Read `assets/workspaces.json` beside this file for the project repository names. Use the current task UUID supplied by DAERI throughout the task and its resumed sessions.

Run the bundled helper with the paths relative to this skill directory:

```sh
python3 scripts/workspace.pyz --config assets/workspaces.json prepare TASK_UUID REPOSITORY --reference BASE_REF
python3 scripts/workspace.pyz --config assets/workspaces.json status TASK_UUID REPOSITORY
python3 scripts/workspace.pyz --config assets/workspaces.json verify TASK_UUID REPOSITORY --reference TARGET_REF --test TEST_COMMAND ARGUMENTS
```

The managed harness provides Python and bash. Git is supplied by the project's custom environment or installed with its approved package manager in this session. The project execution environment supplies the network connection and ordinary Git credentials for the configured remotes.

`prepare` returns a task working directory, branch and pinned base commit. Use that directory for edits, tests and ordinary Git commits. Each selected repository has its own task branch. Repeating `prepare` uses the existing workspace and preserves its changes.

Commit the task changes before `verify`. It merges the task commit into the current target in a disposable checkout, reports conflicts and runs the supplied test command there. Resolve reported conflicts in the task branch and verify again. Push the task branch through ordinary Git when the approved plan includes publishing. Include repository, branch, commit and validation evidence in the result according to the task's needs.

Task files are stored on the configured managed session mount. The task branch pushed to the Git remote is the durable handoff to other tasks or people. After an interrupted Git operation, inspect `status` and the existing branch before continuing.

In a new execution filesystem, `prepare` resumes an existing pushed task branch and reconstructs its base with the merge base of the selected target reference. Unpushed changes remain in the old session filesystem; inspect that session and the previous execution before choosing a recovery path.
