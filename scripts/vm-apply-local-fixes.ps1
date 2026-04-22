$ErrorActionPreference = "Stop"

$projectRoot = "C:\teams-bot-poc"
$serviceFile = Join-Path $projectRoot "src\Services\TeamsCallingBotService.cs"
$publishDir = Join-Path $projectRoot "src\bin\Release\net8.0\publish"

$env:Path = "C:\Program Files\dotnet;C:\ProgramData\chocolatey\bin;$env:Path"

$content = Get-Content $serviceFile -Raw
$pattern = '(?s)await call\.AnswerAsync\(mediaSession(?:,\s*new\[\]\s*\{\s*Modality\.Audio\s*\})?\)\.ConfigureAwait\(false\);'
$replacement = @"
var mediaConfig = new AppHostedMediaConfig
            {
                Blob = mediaSession.GetMediaConfiguration().ToString()
            };

            await call.AnswerAsync(
                    mediaConfig,
                    new[] { Modality.Audio })
                .ConfigureAwait(false);
"@
$updated = [regex]::Replace($content, $pattern, $replacement)

if ($updated -ne $content) {
    Set-Content -Path $serviceFile -Value $updated -Encoding UTF8
}

Set-Location (Join-Path $projectRoot "src")
dotnet publish --configuration Release --output $publishDir

Select-String -Path $serviceFile -Pattern 'AnswerAsync\(mediaSession, new\[\] \{ Modality.Audio \}\)'
