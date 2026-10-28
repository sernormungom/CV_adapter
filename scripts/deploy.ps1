<#
.SYNOPSIS
    Deploy the CV Adapter app to the AWS `dev` environment (ECS Fargate).

.DESCRIPTION
    Wraps the three-step deploy pipeline into one command. See DEPLOY.md for the
    full mental model. In short:

        1. GitHub Actions has already BUILT your image and pushed it to ECR,
           tagged with the commit SHA.  (This script does NOT build — it assumes
           the `build-and-push` workflow finished. It even checks ECR to be sure.)
        2. Terraform REGISTERS a new task-definition revision (`terraform apply`
           with `app_image_tag` set to your SHA). This does NOT roll the running
           service — `infra/modules/service/main.tf` sets
           `ignore_changes = [task_definition, desired_count]` on the ECS
           service specifically so Terraform never fights an out-of-band deploy.
        3. This script then ROLLS the service itself: `aws ecs update-service
           --force-new-deployment` against the revision Terraform just
           registered, then waits for it to go stable. Skipping this step is
           silent — the old image keeps serving traffic with no error anywhere.

    The one-shot Alembic migration task — the same `aws ecs run-task` dance used
    to bootstrap the first admin — ALWAYS runs between steps 2 and 3, unless
    -SkipMigrate is passed. `alembic upgrade head` is idempotent (a no-op when
    the DB is already current), so running it on every deploy costs a few
    seconds and closes off the failure mode where whoever deploys forgets a
    migration is pending: the new code goes live against a schema it doesn't
    match and every request touching the changed table 500s with no warning at
    apply time. Use -SkipMigrate only when you've independently confirmed the
    migration already ran (e.g. re-running after a partial failure).

    This script only READS Terraform state, runs `apply` with an explicit image
    tag, and calls `update-service`/`run-task`. It never runs `terraform destroy`.

.PARAMETER Sha
    The image tag to deploy = the git commit SHA that CI built.
    Defaults to the current HEAD of this repo (`git rev-parse HEAD`), which is
    what you want right after pushing.

.PARAMETER InfraDir
    Path to the infra repo's `envs/dev` folder (where the Terraform state and
    outputs live). Defaults to %USERPROFILE%\repo\infra\envs\dev — override if
    you cloned the infra repo somewhere else.

.PARAMETER SkipMigrate
    Skip the DB migration task. Off by default — migrations run on every
    deploy because they're idempotent, not because one is known to be
    pending. Only pass this when you've already confirmed the DB is current.

.PARAMETER SkipApply
    Skip the Terraform apply AND the force-deployment step; ONLY run the
    migration. Handy when the image is already deployed (service already
    rolled) and you just need to (re)run migrations. Combine with
    -SkipMigrate and there is nothing left to do, which the script refuses.

.PARAMETER AutoApprove
    Pass `-auto-approve` to Terraform (no "yes" prompt). Off by default so you
    always get to eyeball the plan before anything changes.

.EXAMPLE
    .\scripts\deploy.ps1
    Deploy the current commit and run migrations (both are the default now).

.EXAMPLE
    .\scripts\deploy.ps1 -SkipMigrate
    Deploy the current commit without touching the database.

.EXAMPLE
    .\scripts\deploy.ps1 -Sha 9f2a1c...
    Deploy (or roll back to) a specific commit's image.
#>

[CmdletBinding()]
param(
    [string]$Sha,
    [string]$InfraDir = (Join-Path $env:USERPROFILE 'repo\infra\envs\dev'),
    [switch]$SkipMigrate,
    [switch]$SkipApply,
    [switch]$AutoApprove
)

# Stop on the first real error instead of limping onward. Native commands
# (terraform, aws, git) don't throw, so we also check $LASTEXITCODE by hand.
$ErrorActionPreference = 'Stop'

function Assert-LastExit {
    param([string]$What)
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

# ── 1. Figure out WHICH image to deploy ─────────────────────────────────────
# The image tag == the git commit SHA (CI tags every build with `${{ github.sha }}`).
# The repo root is this script's parent's parent (scripts\ -> repo root).
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Sha) {
    $Sha = (git -C $repoRoot rev-parse HEAD).Trim()
    Assert-LastExit "git rev-parse HEAD"
}
Write-Host "==> Target image tag (commit SHA): $Sha" -ForegroundColor Cyan

if ($SkipApply -and $SkipMigrate) {
    throw "-SkipApply together with -SkipMigrate leaves nothing for this script to do."
}

# ── 2. Move into the Terraform env dir ──────────────────────────────────────
# All `terraform output`/`apply` calls must run here — it's where the state and
# backend config live. Push-Location remembers where we came from.
if (-not (Test-Path $InfraDir)) {
    throw "Infra dir not found: $InfraDir`nPass the right path with -InfraDir <path-to-infra\envs\dev>."
}
Push-Location $InfraDir
try {
    # Read the region and ECR repo name straight from Terraform outputs, so this
    # script has no hardcoded environment values (works for staging too).
    $region  = (terraform output -raw region).Trim();          Assert-LastExit "terraform output region"
    $ecrObj  = terraform output -json ecr_repository_urls | ConvertFrom-Json
    $ecrRepo = ($ecrObj.PSObject.Properties.Name)[0]   # e.g. "cv-adapter-app"
    $cluster = (terraform output -raw ecs_cluster_name).Trim(); Assert-LastExit "output ecs_cluster_name"
    $service = (terraform output -raw ecs_service_name).Trim(); Assert-LastExit "output ecs_service_name"

    # ── 3. Pre-flight: confirm CI actually pushed this image ────────────────
    # The #1 mistake is deploying before the GitHub build finished. Ask ECR
    # whether the tag exists; fail early with a clear message if it doesn't.
    Write-Host "==> Checking ECR for $ecrRepo`:$Sha ..." -ForegroundColor Cyan
    aws ecr describe-images --repository-name $ecrRepo `
        --image-ids imageTag=$Sha --region $region 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Image tag '$Sha' is NOT in ECR repo '$ecrRepo' yet.`n" +
              "Has the 'build-and-push' GitHub Action finished? Check the Actions tab.`n" +
              "(HTML-only changes don't trigger it — run it manually if so.)"
    }
    Write-Host "    Image found." -ForegroundColor Green

    # ── 4. Register: point app_image_tag at our SHA and apply ───────────────
    # This registers new task-def revisions (app, migrate) with the new image.
    # It does NOT roll the running service — see the .DESCRIPTION note above
    # and DEPLOY.md. Terraform prompts for 'yes' unless -AutoApprove is set, so
    # you can review the plan first.
    if (-not $SkipApply) {
        Write-Host "==> terraform apply (app_image_tag=$Sha) ..." -ForegroundColor Cyan
        $tfArgs = @('apply', "-var=app_image_tag=$Sha")
        if ($AutoApprove) { $tfArgs += '-auto-approve' }
        terraform @tfArgs
        Assert-LastExit "terraform apply"
        Write-Host "    New task-definition revision registered (service not yet rolled)." -ForegroundColor Green
    }
    else {
        Write-Host "==> Skipping apply (-SkipApply)." -ForegroundColor Yellow
    }

    # ── 5. Run the one-shot DB migration task (default; -SkipMigrate opts out) ──
    # Mirrors the infra repo's `make migrate`, but in PowerShell so it runs
    # natively on Windows. Launches the migrate task family as a Fargate
    # run-task, in the same subnets + security group as the live service (so it
    # can reach RDS), waits for it to stop, and asserts a clean exit code.
    # Deliberately runs BEFORE the app service is rolled (step 6): a migration
    # must land before the new code that depends on it goes live. Runs
    # unconditionally because `alembic upgrade head` is idempotent — the
    # alternative (only run it when someone remembers to ask) is exactly how a
    # deploy once shipped code that expected a column the DB didn't have yet.
    if (-not $SkipMigrate) {
        Write-Host "==> Running database migration task ..." -ForegroundColor Cyan

        $family  = (terraform output -raw migrate_task_family).Trim();  Assert-LastExit "output migrate_task_family"

        # Private subnets come from Terraform; the security group is whatever the
        # running app service uses (guaranteed to have a path to RDS).
        $subnets = ((terraform output -json private_subnets | ConvertFrom-Json) -join ',')
        $sg = (aws ecs describe-services --cluster $cluster --services $service `
                 --region $region `
                 --query "services[0].networkConfiguration.awsvpcConfiguration.securityGroups[0]" `
                 --output text).Trim()
        Assert-LastExit "describe-services (security group lookup)"

        $netCfg = "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=DISABLED}"

        # Launch the migration task and grab its ARN.
        $taskArn = (aws ecs run-task --cluster $cluster --launch-type FARGATE `
                      --task-definition $family --region $region `
                      --network-configuration $netCfg `
                      --query "tasks[0].taskArn" --output text).Trim()
        Assert-LastExit "run-task (migrate)"
        Write-Host "    Migration task: $taskArn" -ForegroundColor Cyan
        Write-Host "    Waiting for it to finish ..." -ForegroundColor Cyan

        # Block until the task stops, then read its exit code. 0 = success.
        aws ecs wait tasks-stopped --cluster $cluster --tasks $taskArn --region $region
        Assert-LastExit "wait tasks-stopped"

        $code = (aws ecs describe-tasks --cluster $cluster --tasks $taskArn `
                   --region $region `
                   --query "tasks[0].containers[0].exitCode" --output text).Trim()
        if ($code -ne '0') {
            throw "Migration FAILED (exit code $code). Check CloudWatch logs at /ecs/$cluster."
        }
        Write-Host "    Migration complete (exit 0)." -ForegroundColor Green
    }

    # ── 6. Actually roll the service ─────────────────────────────────────────
    # terraform apply only registered a new revision (step 4) — the service
    # itself ignores that drift by design (modules/service/main.tf). This is
    # the step that makes the new image (and, if -Migrate ran, the migrated
    # schema) actually live. Skipped when -SkipApply is set, since that flag
    # means the image is already deployed and only a migration was wanted.
    if (-not $SkipApply) {
        Write-Host "==> Rolling the app service onto the new revision ..." -ForegroundColor Cyan

        $latestRevisionArn = (aws ecs list-task-definitions --family-prefix $service `
                                --status ACTIVE --sort DESC --region $region `
                                --query "taskDefinitionArns[0]" --output text).Trim()
        Assert-LastExit "list-task-definitions (app family)"

        aws ecs update-service --cluster $cluster --service $service `
            --task-definition $latestRevisionArn --force-new-deployment `
            --region $region 1>$null
        Assert-LastExit "update-service --force-new-deployment"
        Write-Host "    Deployment triggered on $latestRevisionArn. Waiting for it to stabilize ..." -ForegroundColor Cyan

        aws ecs wait services-stable --cluster $cluster --services $service --region $region
        Assert-LastExit "wait services-stable"
        Write-Host "    Service stable on the new revision." -ForegroundColor Green
    }
    else {
        Write-Host "==> Skipping service rollout (-SkipApply): assuming the image is already live." -ForegroundColor Yellow
    }

    # ── 7. Done — print where to look ───────────────────────────────────────
    $albDns = (terraform output -raw alb_dns_name).Trim()
    Write-Host ""
    Write-Host "Deploy complete. App: http://$albDns/" -ForegroundColor Green
    Write-Host "Deployed image tag: $Sha" -ForegroundColor Green
}
finally {
    # Always return to the original directory, even if something threw above.
    Pop-Location
}
</content>
