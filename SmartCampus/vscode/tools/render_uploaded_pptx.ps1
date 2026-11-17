param([Parameter(Mandatory=$true)][string]$Source,[Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$allowedRoot = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) 'run\uploads')).TrimEnd('\') + '\'
$inputPath = [IO.Path]::GetFullPath($Source)
$outputPath = [IO.Path]::GetFullPath($Destination)
if (-not $inputPath.StartsWith($allowedRoot,[StringComparison]::OrdinalIgnoreCase) -or -not $outputPath.StartsWith($allowedRoot,[StringComparison]::OrdinalIgnoreCase)) { throw 'Rendering paths must stay inside the upload workspace.' }
$office = New-Object -ComObject PowerPoint.Application
$existing = $office.Presentations.Count
$security = $office.AutomationSecurity
$presentation = $null
try {
    $office.AutomationSecurity = 3
    try { $presentation = $office.Presentations.Open($inputPath,-1,0,0) }
    catch { $presentation = $office.Presentations.Open2007($inputPath,-1,0,0,-1) }
    if ($presentation.Slides.Count -gt 120) { throw 'Upload limit is 120 slides.' }
    $presentation.SaveCopyAs($outputPath,32,0)
} finally {
    if ($null -ne $presentation) { $presentation.Close(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation) }
    $office.AutomationSecurity = $security
    if ($existing -eq 0 -and $office.Presentations.Count -eq 0) { $office.Quit() }
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($office)
}
