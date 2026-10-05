[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$sdkRoot = Split-Path -Parent $PSScriptRoot

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}

Push-Location -LiteralPath $sdkRoot
try {
    Get-Command git, uv -ErrorAction Stop | Out-Null
    $changes = & git status --porcelain
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read repository status.' }
    if ($changes) {
        if (-not $DryRun) {
            throw 'Commit the release changes first. Use publish.bat -DryRun to check an uncommitted draft.'
        }
        Write-Host 'Draft checks: the working tree has uncommitted changes. No tag or push will be performed.'
    }
    Invoke-Checked 'git' @('diff', '--check')
    Invoke-Checked 'uv' @('sync', '--locked', '--python', '3.12', '--dev')
    $sdkPython = Join-Path $sdkRoot '.venv/Scripts/python.exe'
    $version = & $sdkPython 'scripts/release_version.py'
    if ($LASTEXITCODE -ne 0) { throw 'Package and public SDK versions do not match.' }
    $version = $version.Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|\.post\d+|\.dev\d+)?$') {
        throw "Unsupported release version: $version"
    }
    $tag = "v$version"
    Invoke-Checked 'git' @('check-ref-format', "refs/tags/$tag")

    if (-not $DryRun) {
        $branch = & git symbolic-ref --quiet --short HEAD
        if ($LASTEXITCODE -ne 0) { throw 'Release from a branch, not a detached HEAD.' }
        $origin = & git remote get-url origin
        if ($LASTEXITCODE -ne 0) { throw 'The origin remote is missing.' }
        if ($origin -notmatch '^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)capemeta/genesis-sandbox-client-python(?:\.git)?/?$') {
            throw 'origin must point to capemeta/genesis-sandbox-client-python.'
        }
        $localTag = & git tag --list $tag
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read local tags.' }
        if ($localTag) { throw "Tag $tag already exists. Do not overwrite a release tag." }
        $remoteTag = & git ls-remote --tags origin "refs/tags/$tag"
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect remote tags.' }
        if ($remoteTag) { throw "Remote tag $tag already exists. Bump the package version." }
    }

    Invoke-Checked $sdkPython @('-m', 'ruff', 'check', '.')
    Invoke-Checked $sdkPython @('-m', 'mypy', 'src/genesis_sandbox_client')
    Invoke-Checked $sdkPython @('-m', 'pytest', '-q')

    $runId = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
    $checkRoot = Join-Path $sdkRoot ".release-check/$version-$runId"
    $distDirectory = Join-Path $checkRoot 'dist'
    $installDirectory = Join-Path $checkRoot 'installed'
    Invoke-Checked 'uv' @('build', '--out-dir', $distDirectory)
    $wheel = Join-Path $distDirectory "genesis_sandbox_client_python-$version-py3-none-any.whl"
    $sourceArchive = Join-Path $distDirectory "genesis_sandbox_client_python-$version.tar.gz"
    Invoke-Checked 'uv' @('tool', 'run', '--from', 'twine==6.2.0', 'twine', 'check', $wheel, $sourceArchive)
    Invoke-Checked 'uv' @('pip', 'install', '--python', $sdkPython, '--target', $installDirectory, '--no-deps', $wheel)
    Invoke-Checked $sdkPython @('-I', 'scripts/verify_release.py', $installDirectory, $wheel)

    if ($DryRun) {
        Write-Host "Checks passed for $version. No Git tag, Git push, or PyPI upload was performed."
        Write-Host "Artifacts: $distDirectory"
        exit 0
    }

    $changes = & git status --porcelain
    if ($LASTEXITCODE -ne 0 -or $changes) { throw 'Working tree changed during verification. Commit and check again.' }
    Invoke-Checked 'git' @('push', 'origin', 'HEAD')
    Invoke-Checked 'git' @('tag', '-a', $tag, '-m', "Release Python SDK $version")
    Invoke-Checked 'git' @('push', 'origin', "refs/tags/$tag")
    Write-Host "Submitted $tag. GitHub Actions will build and publish the committed source."
    Write-Host 'Check build AND publish at https://github.com/capemeta/genesis-sandbox-client-python/actions'
    Write-Host 'A successful tag push does not mean the PyPI upload has completed.'
}
catch {
    Write-Host "Release stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'If the tag push failed after tag creation, inspect the tag and retry its push; do not overwrite it.'
    exit 1
}
finally {
    Pop-Location
}
