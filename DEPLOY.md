# Deploying CV Adapter to AWS

How a code change on your laptop becomes a running change on the cloud.

> **Audience:** developers shipping to the `dev` environment on AWS
> (ECS Fargate, region `eu-north-1`). If you're setting the environment up from
> scratch, that lives in the separate **infra** repo
> (`mpya-sci-and-tech/infra`) — this doc only covers *deploying the app*.

---

## The one thing to understand: build ≠ deploy ≠ roll out

Deployment is a **three-step pipeline split across two tools**, and the third
step is the one people miss. Getting this straight saves a lot of confusion:

1. **GitHub Actions builds the image — but only behind the gates.** On push to
   `main`, the `build-and-push` workflow (`.github/workflows/build-push.yml`)
   first runs every test gate by calling `.github/workflows/ci.yml`, and only
   then builds a Docker image and pushes it to **ECR** (AWS's private image
   registry), tagged with the **commit SHA** (e.g. `cv-adapter-app:9f2a1c...`).
   It does **not** touch the running service.

   The build job declares `needs: gates`, so a failing fast, static,
   integration, or docker gate means the build job never starts and **nothing is
   pushed to ECR**. This workflow once ran no tests at all. Do not add a
   gate-skipping condition to get a deploy out.

2. **Terraform registers the new task definition — but does not roll the
   service.** The infra repo defines the image every task runs as
   `cv-adapter-app:${app_image_tag}`. `app_image_tag` is a Terraform variable.
   You point it at the new SHA and run `terraform apply`; Terraform registers a
   new **task-definition revision** (app + migrate families) with that image.
   That's all it does to the running service — **the ECS service resource
   itself is untouched by this apply.**

3. **A separate `aws ecs update-service --force-new-deployment` actually rolls
   it out.** `infra/modules/service/main.tf` sets
   `lifecycle { ignore_changes = [task_definition, desired_count] }` on the
   `aws_ecs_service` resource, specifically so Terraform never fights a
   deployment made out-of-band — but the effect is that **`terraform apply`
   registering a new revision changes nothing about what's actually running.**
   Nothing runs this for you automatically; you (or `deploy.ps1`, see below)
   must call `update-service` with `--force-new-deployment` yourself, pointed
   at the revision Terraform just registered. Skipping this step is silent: the
   apply succeeds, the plan looks right, and the live task keeps serving the
   old image indefinitely.

So the rule (straight from the infra README): **image-tag handling lives in
Terraform, not CI** — but *rollout* lives in neither; it's this explicit third
command. That's also why the build workflow has no `update-service` step of
its own — CI's job ends at "the image exists in ECR."

```
  git push main
        │
        ▼
  GitHub Actions  ──gates──►  ──build──►  ECR: cv-adapter-app:<sha>
  (build-and-push)   fast/static/          (skipped if any gate is red)
                     integration/docker
        │
        ▼   (you run this)
  terraform apply -var="app_image_tag=<sha>"
        │
        ▼
  New task-definition revision registered — service NOT yet updated
  (ignore_changes on the aws_ecs_service resource)
        │
        ▼   (you must also run this — it is not automatic)
  aws ecs update-service --cluster cv-adapter-dev --service cv-adapter-dev \
    --task-definition cv-adapter-dev:<new-revision> --force-new-deployment
        │
        ▼
  ECS rolling deploy  ──►  live at the ALB URL
```

**How to tell you actually forgot this:** `terraform apply` exiting 0 proves
nothing about what's live. Check the service's real rollout state:

```powershell
aws ecs describe-services --cluster cv-adapter-dev --services cv-adapter-dev `
  --region eu-north-1 `
  --query "services[0].deployments[*].{status:status,taskDefinition:taskDefinition,rolloutState:rolloutState}"
```

If `PRIMARY` still names the *old* revision, the service never rolled. Hitting
`/health` is not enough to catch this — the old task answers `/health` just
fine.

---

## Prerequisites (one-time)

You need the same local setup used for any AWS work here:

- **AWS CLI** configured and authenticated (`aws sts get-caller-identity`
  returns your user in account `938184884537`).
- **Terraform CLI** installed.
- The **infra repo cloned**, and `terraform init` already run once inside
  `infra/envs/dev` (connects to the shared remote state).
- For CI to build at all, the app repo must have (set by the infra repo's
  `make gh-apply`): secret `AWS_CI_ROLE_ARN`, variables `AWS_REGION` and
  `ECR_REPOSITORY`. Verify under **repo Settings → Secrets and variables →
  Actions** if a build ever fails at the "Configure AWS credentials" step.

---

## The happy path (every deploy)

### Step 1 — Push, and let CI build the image

```powershell
git push origin main
```

Watch the **Actions** tab until `build-and-push` is green. It runs the gates
first (`fast`, `static`, `integration`, `docker`) and then builds; green means
every gate passed **and** `cv-adapter-app:<your-commit-sha>` now exists in ECR.

If the run is red, read which gate failed and fix that. A red gate means no
image was pushed, so there is nothing to deploy — and the fix is the failing
gate, never a change to the workflow (M1 rollback rule; see
[docs/testing.md](docs/testing.md)).

> **HTML/CSS/Alembic changes do trigger a build.** They used to be excluded
> while the Dockerfile still did `COPY . .`, so an HTML-only fix was never built
> or deployed — fixed in V2 M1. Documentation-only commits are still skipped
> deliberately; if you need an image for one, use
> **Actions tab → build-and-push → Run workflow**, or
> `gh workflow run build-push.yml`.

### Step 2 — Find the SHA you just built

```powershell
git rev-parse HEAD
```

This full 40-character SHA is your image tag — CI tags images with exactly this
value (`${{ github.sha }}`).

### Step 3 — Register the new task definition with Terraform

From `infra/envs/dev`:

```powershell
terraform apply -var="app_image_tag=<paste-sha>"
```

Terraform prints a plan — you'll see the app and migrate **task definitions**
replaced (a new revision each), but **no change to the `aws_ecs_service`
resource itself**. That's expected (see above), not a sign the apply is
incomplete. Type `yes`.

### Step 4 — Actually roll the service (do not skip this)

Terraform did not do this for you. Find the revision it just registered and
force the deployment:

```powershell
aws ecs list-task-definitions --family-prefix cv-adapter-dev --status ACTIVE `
  --sort DESC --region eu-north-1 --query "taskDefinitionArns[0]" --output text

aws ecs update-service --cluster cv-adapter-dev --service cv-adapter-dev `
  --task-definition cv-adapter-dev:<revision-from-above> --force-new-deployment `
  --region eu-north-1

aws ecs wait services-stable --cluster cv-adapter-dev --services cv-adapter-dev `
  --region eu-north-1
```

`wait services-stable` blocks until the new task is healthy and the old one is
drained. When it returns, your change is live at:

```
http://<alb-dns-name>.eu-north-1.elb.amazonaws.com/
```

Then confirm `describe-services` (below) names the **new** revision as
`PRIMARY`, and watch the service's CloudWatch logs for a few minutes for 5xx
responses or startup errors before calling the deploy done.

**Or do Steps 2–4 in one command** with the helper (see below):

```powershell
.\scripts\deploy.ps1
```

> **As of this writing, `deploy.ps1` only performs Step 3 (`terraform apply`)
> — it does not yet run Step 4's `update-service --force-new-deployment`.**
> Until the script is updated, run Step 4 by hand after the helper finishes, or
> use the manual sequence above end to end. Trusting the helper alone will
> leave the old image running with no error at any point in the process.

---

## Required task-definition environment

Two settings live in the **infra repo's** ECS task definition, not here, and
neither is verifiable from this repository:

| Variable | Value | Why it matters |
| --- | --- | --- |
| `APP_ENV` | `production` | This is what makes `AUTH_ENFORCED=false` a hard startup failure instead of a merely-discouraged setting (`backend/config.py`). Without it the deployed app still defaults to auth-on, but the guard that *prevents* disabling auth is inert. |
| `AUTH_ENFORCED` | `true` (or unset) | Defaults to true. Never set it to `false` in a deployed environment. |

Several active Sites are normal: the seed provisions four (GBG, MAL, STH,
UPS), and nothing at startup restricts the Site count. Zero Sites is valid too
(a migrated-but-unseeded database), but no account can be provisioned until a
Site exists. For public sharing over the internet, also set
`SESSION_COOKIE_SECURE=true` (it enables the secure cookie and HSTS) once the
service is behind TLS — see `docs/product.md` for the other prerequisites.

Seeding the pilot cast is a separate, explicit one-shot task —
`make seed-pilot` with `PILOT_SEED_PASSWORD` exported, never passed as a make
variable or a task-definition literal. Migrations create schema only.

---

## If your change includes a database migration

New Alembic migration in the code? The **migrate** task uses the same image
tag, so:

1. `terraform apply -var="app_image_tag=<sha>"` — this registers a new
   migrate task-def revision with the new image. Unlike the app service, the
   migrate **task family has no `aws_ecs_service`** — it's launched directly
   by `run-task` against the family name, which always resolves to the latest
   *active* revision automatically. So this step alone is sufficient to make
   the next migrate run use the new image; no separate "roll" step exists for
   it the way there is for the app.
2. Run the one-shot migration task. This is the exact `run-task` dance used to
   bootstrap the admin, wrapped up for you:

   ```powershell
   .\scripts\deploy.ps1 -Migrate
   ```

   (Under the hood: `aws ecs run-task` on the `cv-adapter-dev-migrate` task
   family → wait for it to stop → assert exit code 0.)
3. Force the app service onto the same revision (Step 4 above) — `-Migrate`
   does not do this either. Forgetting it means the migration lands but the
   app answering traffic is still the old code against the new schema.

> **Migration ordering matters.** For **backward-compatible** migrations
> (adding a column/table), migrating then rolling the service is fine either
> order. For a **destructive** migration the old code can't tolerate, do not
> force the app service deployment (Step 4) until the migration has already
> run — the old task must not touch the new schema. Run the migrate task
> first, confirm exit code 0, *then* force the app deployment. Getting this
> backwards is exactly how a live app ends up querying a schema it doesn't
> match. This is not hypothetical: during the P-019 AWS reset (the app data was
> wiped and the current schema deployed clean), `terraform apply` was run and treated as sufficient;
> the app service silently kept serving the pre-reset image through an RDS
> wipe and a fresh `alembic upgrade head`, so every login 500'd — the old
> code was querying `users.role`, a column the new V2 schema doesn't have. The
> fix was the missing `update-service --force-new-deployment` call, above.

---

## Gotcha: pin the image tag so it can't silently revert

`app_image_tag` **defaults to `latest`**, but CI pushes SHA tags, not `latest`.
So if you ever run a bare `terraform apply` (no `-var`), Terraform tries to roll
the service *back* to `:latest` — the "revision drift" the infra README warns
about.

**Recommended:** keep the current tag in a `terraform.tfvars` file so every
apply is consistent and you can't forget the flag. In `infra/envs/dev`:

```hcl
# infra/envs/dev/terraform.tfvars
app_image_tag = "9f2a1c..."   # bump this to the new SHA on each deploy
```

With that file present, plain `terraform apply` always deploys the pinned tag.
(`deploy.ps1` passes `-var` explicitly, so it's safe either way — but the
tfvars file protects you when you run Terraform by hand.) Either way, this
only decides what revision `apply` *registers* — it does not, by itself, put
that revision on the running service. Step 4 still applies.

---

## Rolling back

A rollback is just a deploy of an older SHA — Terraform doesn't care whether the
tag is newer or older, and neither does `update-service`:

```powershell
terraform apply -var="app_image_tag=<older-good-sha>"
# or: .\scripts\deploy.ps1 -Sha <older-good-sha>

aws ecs list-task-definitions --family-prefix cv-adapter-dev --status ACTIVE `
  --sort DESC --region eu-north-1 --query "taskDefinitionArns[0]" --output text

aws ecs update-service --cluster cv-adapter-dev --service cv-adapter-dev `
  --task-definition cv-adapter-dev:<revision-from-above> --force-new-deployment `
  --region eu-north-1
```

Find previous SHAs in your git log (`git log --oneline`) or list what's in ECR:

```powershell
aws ecr describe-images --repository-name cv-adapter-app --region eu-north-1 `
  --query "sort_by(imageDetails,&imagePushedAt)[*].imageTags" --output table
```

---

## Quick reference

| Task | Command (from `infra/envs/dev`, or use the helper) |
|---|---|
| Deploy latest commit (register only — see caveat below) | `.\scripts\deploy.ps1` |
| Deploy + migrate (register + migrate only) | `.\scripts\deploy.ps1 -Migrate` |
| Deploy a specific SHA (register only) | `.\scripts\deploy.ps1 -Sha <sha>` |
| Register the task def by hand | `terraform apply -var="app_image_tag=<sha>"` |
| **Actually roll the service (required, every time)** | `aws ecs update-service --cluster cv-adapter-dev --service cv-adapter-dev --task-definition cv-adapter-dev:<rev> --force-new-deployment --region eu-north-1` |
| Wait for rollout to finish | `aws ecs wait services-stable --cluster cv-adapter-dev --services cv-adapter-dev --region eu-north-1` |
| Check what's actually live | `aws ecs describe-services --cluster cv-adapter-dev --services cv-adapter-dev --region eu-north-1 --query "services[0].deployments[*].{status:status,taskDefinition:taskDefinition,rolloutState:rolloutState}"` |
| Run migrations only | `.\scripts\deploy.ps1 -Sha <current-sha> -Migrate -SkipApply` |
| See current outputs | `terraform output` |
| App URL | `terraform output -raw alb_dns_name` |

> **`deploy.ps1` does not currently run the "actually roll the service" step**
> (see the Step 4 callout above) — treat its output as "image registered,"
> not "image live," until the script is updated to call `update-service`
> itself.
</content>
</invoke>
