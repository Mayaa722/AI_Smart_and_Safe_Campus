param([Parameter(Mandatory = $true)][string]$Source, [Parameter(Mandatory = $true)][string]$Destination)
# Windows fallback when LibreOffice is not installed: export PPTX via PowerPoint, DOCX via Word, to PDF.
# Macros are disabled while the file is open; the app quits only if it was not already running.
$ErrorActionPreference = 'Stop'
$inputPath = [IO.Path]::GetFullPath($Source)
$outputPath = [IO.Path]::GetFullPath($Destination)
$extension = [IO.Path]::GetExtension($inputPath).ToLowerInvariant()

if ($extension -eq '.pptx') {
    $app = New-Object -ComObject PowerPoint.Application
    $before = $app.Presentations.Count
    $security = $app.AutomationSecurity
    $file = $null
    try {
        $app.AutomationSecurity = 3
        $file = $app.Presentations.Open($inputPath, -1, 0, 0)   # read-only, no title, no window
        $file.SaveCopyAs($outputPath, 32)                         # 32 = ppSaveAsPDF
    } finally {
        if ($null -ne $file) { $file.Close() }
        $app.AutomationSecurity = $security
        if ($before -eq 0 -and $app.Presentations.Count -eq 0) { $app.Quit() }
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($app)
    }
} elseif ($extension -eq '.docx') {
    $app = New-Object -ComObject Word.Application
    $before = $app.Documents.Count
    $file = $null
    try {
        $app.Visible = $false
        $app.AutomationSecurity = 3
        $file = $app.Documents.Open($inputPath, $false, $true)    # no conversion dialog, read-only
        $file.ExportAsFixedFormat($outputPath, 17)                 # 17 = wdExportFormatPDF
    } finally {
        if ($null -ne $file) { $file.Close(0) }
        if ($before -eq 0) { $app.Quit() }
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($app)
    }
} else {
    throw "Unsupported file type: $extension"
}
