# Commit, Push, and Restart Webcam Bot

## Goal
Commit the current project changes, push them to the current branch's remote, and restart the Docker Compose service so the latest bot code runs.

## Stages

### Stage 1: Review and Commit Changes
Inspect repository state, stage relevant files, and create a commit message aligned with the changes made.

#### mini-researcher
Collect `git status`, `git diff`, and recent commit message style to prepare a safe, accurate commit.

#### mini-implementer
Stage tracked and new project files (excluding secrets), create the commit, and verify clean post-commit status.

### Stage 2: Push and Restart Service
Push the commit to the current branch's configured remote and restart the containerized bot.

#### mini-implementer
Push to the current remote branch and run Docker Compose restart flow (`up -d --build`) to deploy the updated container.

#### mini-tester
Verify container/service status after restart and report whether the bot process is running.
