# Pulls the latest weekly export produced by the GitHub Actions workflow
# and copies it into iCloud Drive. Cheap and safe to run often — it does
# not scrape anything itself, so timing isn't critical, only that it runs
# at some point after the PC is on.

$ErrorActionPreference = "Stop"

$repo   = "C:\Users\user\repos\grocery-tracker"
$icloud = "C:\Users\user\iCloudDrive\grocery_deals"

Set-Location $repo
git fetch origin main
git merge --ff-only origin/main

New-Item -ItemType Directory -Force -Path $icloud | Out-Null
Copy-Item -Path (Join-Path $repo "exports\*") -Destination $icloud -Recurse -Force
