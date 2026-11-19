# Pester 5 tests for Publish-ReadyPackageViaDocker (Docker-Index.ps1).
# Run: Import-Module Pester -MinimumVersion 5.0; Invoke-Pester -Path tools/common/Docker-Index.Tests.ps1

BeforeAll {
    . (Join-Path $PSScriptRoot 'Docker-Index.ps1')
}

Describe 'Publish-ReadyPackageViaDocker' {
    BeforeEach {
        $script:ready = Join-Path $TestDrive ("ready-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Force -Path (Join-Path $script:ready 'src') | Out-Null
        Set-Content -LiteralPath (Join-Path $script:ready 'src\Configuration.xml') -Value '<Configuration/>' -Encoding Ascii
        Set-Content -LiteralPath (Join-Path $script:ready 'manifest.json') -Value '{}' -Encoding Ascii
        [IO.File]::WriteAllBytes((Join-Path $script:ready 'READY'), [byte[]]@())
        $script:calls = [System.Collections.Generic.List[object]]::new()
    }

    It 'refuses package without READY' {
        Remove-Item -LiteralPath (Join-Path $script:ready 'READY') -Force
        {
            Publish-ReadyPackageViaDocker `
                -ReadyPathWin $script:ready `
                -LinuxInbox '/srv/ai-contour/inbox/kb' `
                -DockerCall { param($Args) @{ ExitCode = 0; StdOut = '' } }
        } | Should -Throw '*READY*'
    }

    It 'refuses Linux inbox outside /srv/ai-contour/inbox/' {
        {
            Publish-ReadyPackageViaDocker `
                -ReadyPathWin $script:ready `
                -LinuxInbox '/tmp/evil' `
                -DockerCall { param($Args) @{ ExitCode = 0; StdOut = '' } }
        } | Should -Throw '*inbox*'
    }

    It 'copies via docker volume, chowns, and renames into ready-*' {
        $name = Split-Path -Leaf $script:ready
        $DockerCall = {
            param([string[]]$DockerArgs)
            $script:calls.Add([string[]]$DockerArgs)
            if ($DockerArgs[0] -eq 'create') {
                return @{ ExitCode = 0; StdOut = 'cid-test' }
            }
            if ($DockerArgs[0] -eq 'run') {
                return @{ ExitCode = 0; StdOut = "DELIVER_OK`n12" }
            }
            return @{ ExitCode = 0; StdOut = '' }
        }

        Publish-ReadyPackageViaDocker `
            -ReadyPathWin $script:ready `
            -LinuxInbox '/srv/ai-contour/inbox/kb' `
            -Owner '999:988' `
            -DockerCall $DockerCall

        $joined = ($script:calls | ForEach-Object { ($_ -join ' ') }) -join "`n"
        $joined | Should -Match 'volume create'
        $joined | Should -Match ([regex]::Escape("cp $($script:ready)/."))
        $joined | Should -Match 'chown -R 999:988'
        $joined | Should -Match ([regex]::Escape("NAME=$name"))
        $joined | Should -Match 'incoming-\$NAME'
        $joined | Should -Match 'volume rm'
    }

    It 'throws when docker run delivery fails' {
        $DockerCall = {
            param([string[]]$DockerArgs)
            if ($DockerArgs[0] -eq 'create') {
                return @{ ExitCode = 0; StdOut = 'cid-x' }
            }
            if ($DockerArgs[0] -eq 'run') {
                return @{ ExitCode = 42; StdOut = 'boom' }
            }
            return @{ ExitCode = 0; StdOut = '' }
        }
        {
            Publish-ReadyPackageViaDocker `
                -ReadyPathWin $script:ready `
                -LinuxInbox '/srv/ai-contour/inbox/kb' `
                -DockerCall $DockerCall
        } | Should -Throw '*deliver*'
    }
}
