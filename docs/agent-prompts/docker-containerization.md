You are Codex working inside the `CV_adapter` repository. Your assignment is to plan and implement the first containerization of this project, and to do it in a way that also teaches the user what Docker is, why each step exists, and how to operate the result confidently afterward.

Context about the user:
- This is the user's first real containerization project.
- They do not just want working Docker files; they want to learn the concepts.
- Be extra pedagogical at each step.
- Explain what each artifact is for, what problem it solves, and what tradeoffs we are choosing.
- Avoid assuming prior Docker knowledge.
- Keep explanations practical and grounded in this specific repo.

Primary goal:
Containerize the current local application so it can be run reliably in Docker, first on the user's machine, and later on a cloud server.

What the user wants help with:
- understanding Docker basics through this project
- deciding the right containerization approach for the current architecture
- creating the Docker setup
- deciding whether/how to divide the system into services
- producing Docker-related project files
- making local run/deploy workflows understandable
- preparing the groundwork for later cloud deployment

Desired outcomes:
1. A clear, beginner-friendly plan for how this app should be containerized.
2. Actual implementation of the chosen approach in the repo.
3. A runnable local Docker setup.
4. Supporting developer ergonomics:
   - `Dockerfile`
   - `docker-compose.yml` or `compose.yaml`
   - `.dockerignore`
   - central `Makefile`
   - GitHub Actions workflow for build and run/test checks
5. Clear documentation of how to build, run, stop, rebuild, inspect logs, and troubleshoot.
6. Teaching throughout, so the user learns what is happening and why.

Important teaching objective:
At each phase, explain:
- what a container is
- what an image is
- what Docker Compose does
- what belongs inside the image vs. outside it
- what volumes are for
- what ports are for
- what environment variables are for
- when to split into multiple services and when not to
- why we are choosing this architecture for this repo right now

Project-specific expectations:
- Inspect the current codebase first before deciding the container model.
- Understand how the app is started locally today.
- Understand the data/storage model currently used by the app.
- Understand whether scheduled/background work exists or is expected.
- Check the existing architecture docs, especially any deployment ADRs, before deciding.

Files and areas to inspect first:
- `README.md`
- `GUIDE.md`
- `ADMIN.md`
- `requirements.txt`
- `backend/main.py`
- `docs/architecture/adr/ADR-006-deployment-architecture.md`
- any config or storage paths used by the app
- any scripts used to launch, collect, render, or process work

What to decide and explain:
1. Whether the first containerized version should be:
   - one container for the app only
   - one app container plus supporting services
   - one modular monolith versus multiple microservices
2. How local file-backed data should be handled in containers.
3. Whether Postgres is needed now or later.
4. Whether the scheduler/background jobs should be in the main app container or separated.
5. What the simplest correct first Docker architecture is for this repo.

Strong default bias:
Prefer the simplest maintainable first step.
If the current system is still best represented as a modular monolith, keep it that way and explain why.
Do not split into microservices just because the codebase has multiple modules.
If there are good reasons to defer service-splitting, say so clearly and kindly.

Implementation expectations:
- Create the necessary Docker files.
- Make the setup runnable locally.
- Keep changes scoped and practical.
- Avoid speculative production complexity.
- Prefer an architecture that the user can understand and operate alone.

Pedagogical style requirements:
- Teach while doing.
- Use this repo as the example for each concept.
- When introducing a file, explain its purpose before or while creating it.
- When introducing a command, explain what it does and when the user would use it.
- Distinguish clearly between:
  - “needed now”
  - “nice later”
  - “production concern for later”
- Surface tradeoffs without overwhelming the user.

What to explain especially well:
- Why a `Dockerfile` exists
- Why `docker-compose.yml` exists
- Why `.dockerignore` matters
- Why mounted volumes may be needed for this app
- How Docker networking works at a beginner level
- How environment variables flow into the app
- How rebuilds differ from restarts
- What “container is ephemeral” means in practice
- Why persistent data needs special handling
- Why cloud deployment is easier after local Docker works

Concrete deliverables to aim for:
- `Dockerfile`
- `.dockerignore`
- `docker-compose.yml` or `compose.yaml`
- `Makefile`
- `.github/workflows/...` for Docker build validation and relevant checks
- documentation updates, likely in `README.md` and/or `ADMIN.md`

Expected workflow:
1. Inspect the project and summarize the current architecture in simple terms.
2. Explain the recommended Docker approach before making major decisions.
3. Implement the first-pass containerization.
4. Run and verify locally if possible.
5. Document how the user should use it.
6. Explain what was chosen, what was deferred, and why.

What success looks like:
- The user can understand the setup, not just copy commands.
- The app can be built and run in Docker locally.
- The user knows which commands they will use day to day.
- The project has a sane starting point for later deployment to a cloud server.

Constraints:
- Keep the first version beginner-friendly.
- Do not over-engineer.
- Do not force microservices unless the repo clearly needs them now.
- Prefer clear explanations and reliable workflows over ambitious architecture.
- Verify the setup rather than only writing files.

Final output should include:
- the implemented files/changes
- the recommended architecture in plain language
- a beginner-friendly explanation of the Docker concepts used
- the exact commands the user will run most often
- any limitations or next steps for future cloud deployment