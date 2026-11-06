$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Fail-Ocr([string]$Code) {
    @{ error = $Code } | ConvertTo-Json -Compress
    exit 0
}

$stream = $null
$random = $null
$bitmap = $null
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    function Wait-WinRT($Operation, [type]$ResultType) {
        $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
        $task.GetAwaiter().GetResult()
    }

    $language = [Windows.Globalization.Language]::new('zh-Hans-CN')
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) { Fail-Ocr 'language' }

    $bytes = [Convert]::FromBase64String([Console]::In.ReadToEnd())
    $stream = [IO.MemoryStream]::new($bytes, $false)
    $random = [IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
    $decoder = Wait-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($random)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $limit = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if ($decoder.PixelWidth -gt $limit -or $decoder.PixelHeight -gt $limit) { Fail-Ocr 'dimensions' }
    $bitmap = Wait-WinRT ($decoder.GetSoftwareBitmapAsync(
        [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
        [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore
    )) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Wait-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    @{ text = ($result.Lines | ForEach-Object { $_.Text }) -join "`n" } | ConvertTo-Json -Compress
} catch {
    Fail-Ocr 'failed'
} finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $random) { $random.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
