# ============================================================
#  LOL LCU 诊断10：503 深挖（战绩页触发 + 重试 + 扫描全部战绩端点）
#  只读诊断，不改任何数据。请【右键 diagnose10.bat -> 以管理员身份运行】
#  前提：LOL 客户端开着并已登录（建议先做：托盘右键完全退出 LOL，再重开客户端登录，然后跑本诊断）
# ============================================================

$ErrorActionPreference = 'Continue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

Write-Host ''
Write-Host '======== LOL LCU 诊断10：战绩 503 深挖 ========'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host '[!] 当前非管理员权限，读取客户端命令行可能失败，建议关闭后右键以管理员身份运行' -ForegroundColor Yellow }
Write-Host ''

# [1] 端口与令牌
$ux = Get-CimInstance Win32_Process -Filter "Name='LeagueClientUx.exe'" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ux -or -not $ux.CommandLine) {
  Write-Host '[X] 读不到 LeagueClientUx.exe：请以管理员身份运行本诊断，并保持 LOL 客户端开启已登录。'
  Read-Host '按回车键关闭'
  exit 1
}
$port = $null; $token = $null
if ($ux.CommandLine -match '--app-port=(\d+)') { $port = $Matches[1] }
if ($ux.CommandLine -match '--remoting-auth-token=([A-Za-z0-9_:=+-]+)') { $token = $Matches[1] }
if (-not $port -or -not $token) {
  Write-Host '[X] 端口/令牌解析失败，请重启 LOL 客户端后再试。'
  Read-Host '按回车键关闭'
  exit 1
}
$b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("riot:$token"))
$desk = [Environment]::GetFolderPath('Desktop')
$bootTime = $ux.CreationDate
if ($bootTime) {
  $bt = $bootTime
  if ($bt -isnot [datetime]) { $bt = [System.Management.ManagementDateTimeConverter]::ToDateTime($bt) }
  Write-Host ("[1] LCU 端口={0}  客户端启动于: {1}" -f $port, $bt.ToString('MM-dd HH:mm:ss'))
  $uptime = (Get-Date) - $bt
  Write-Host ("    客户端已运行: {0} 小时 {1} 分钟" -f [int]$uptime.TotalHours, $uptime.Minutes)
  if ($uptime.TotalMinutes -lt 3) { Write-Host '    >>> 客户端刚启动 3 分钟内，战绩服务可能还没就绪，稍等片刻再继续' -ForegroundColor Yellow }
} else {
  Write-Host ("[1] LCU 端口={0}" -f $port)
}
Write-Host ''

# [2] 当前账号
$t0 = Join-Path $env:TEMP 'lcu_summoner10.json'
if (Test-Path $t0) { Remove-Item $t0 -Force }
& curl.exe -k -s -m 12 -H "Authorization: Basic $b64" -o $t0 "https://127.0.0.1:$port/lol-summoner/v1/current-summoner" 2>$null
$s0 = ''
if ((Test-Path $t0) -and ((Get-Item $t0).Length -gt 0)) { $s0 = [IO.File]::ReadAllText($t0) }
$accountName = ''; $puuid = ''; $accountId = ''
if ($s0 -and ($s0 -notmatch 'errorCode')) {
  $sj = $null
  try { $sj = $s0 | ConvertFrom-Json } catch {}
  if ($sj) {
    $accountName = [string]$sj.displayName
    if (-not $accountName) { $accountName = [string]$sj.gameName }
    $puuid = [string]$sj.puuid
    $accountId = [string]$sj.accountId
  }
}
Write-Host ("[2] 当前账号: {0}  puuid={1}" -f $accountName, $puuid)
Write-Host ''

# [3] 关键：先让客户端 UI 自己刷战绩，再探测
Write-Host '[3] 请现在在 LOL 客户端里点开【生涯】-【战绩记录】页，等 10 秒让列表尝试加载。' -ForegroundColor Cyan
Write-Host '    然后回答（输入数字后回车）：'
Write-Host '      1 = 战绩页能看到历史对局列表'
Write-Host '      2 = 战绩页空白/一直转圈/报错'
$uist = Read-Host '    你的观察是'
$uiOk = ($uist -eq '1')
$uiState = '能显示'
if (-not $uiOk) { $uiState = '不能显示(空白/转圈)' }
Write-Host ("    已记录: 客户端战绩页 {0}" -f $uiState)
Write-Host ''

# [4] 核心端点带重试探测（503 可能是瞬时的，隔 5 秒连试 3 次）
Write-Host '[4] 核心战绩端点重试探测（连试 3 次，每次间隔 5 秒）：'
$coreUrl = "https://127.0.0.1:$port/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=20"
$coreOk = $false
for ($try = 1; $try -le 3; $try++) {
  $tf = Join-Path $env:TEMP 'lcu_core10.json'
  if (Test-Path $tf) { Remove-Item $tf -Force }
  $code = (& curl.exe -k -s -m 12 -H "Authorization: Basic $b64" -o $tf -w '%{http_code}' $coreUrl 2>$null | Select-Object -Last 1)
  if (-not $code) { $code = 'ERR' }
  $body = ''
  if ((Test-Path $tf) -and ((Get-Item $tf).Length -gt 0)) { $body = [IO.File]::ReadAllText($tf) }
  $mark = ''
  if ($body -match 'gameId' -and $body -notmatch 'errorCode') { $mark = '  <- ★ 有数据'; $coreOk = $true }
  elseif ($code -eq '503') { $mark = '  <- 503 战绩服务未就绪' }
  elseif ($body -match 'errorCode') { $mark = '  <- 错误响应' }
  Write-Host ("    第{0}次  HTTP={1}  {2}字符{3}" -f $try, $code, $body.Length, $mark)
  if ($coreOk) { break }
  if ($try -lt 3) { Start-Sleep -Seconds 5 }
}
Write-Host ''
if ($coreOk) {
  $out = "诊断结论：接口已恢复（客户端战绩页 {0}）。请重新运行 lol_sync.bat 完成同步。" -f $uiState
  Write-Host $out -ForegroundColor Green
  Read-Host '按回车键关闭'
  exit 0
}

# [5] 从 /Help 扫出客户端里所有 match-history 家族路由（文本匹配，不解析大 JSON）
Write-Host '[5] 扫描客户端全部战绩类路由（/Help）：'
$hf = Join-Path $env:TEMP 'lcu_help10.json'
if (Test-Path $hf) { Remove-Item $hf -Force }
& curl.exe -k -s -m 20 -H "Authorization: Basic $b64" -o $hf "https://127.0.0.1:$port/Help" 2>$null
$helpRaw = ''
if ((Test-Path $hf) -and ((Get-Item $hf).Length -gt 0)) { $helpRaw = [IO.File]::ReadAllText($hf) }
$mhPaths = @()
if ($helpRaw) {
  $mhPaths = @([regex]::Matches($helpRaw, '/lol-match-history/[a-zA-Z0-9_/\-\.\{\}\?=&]+') | ForEach-Object { $_.Value } | Sort-Object -Unique)
}
$extraPaths = @()
if ($helpRaw) {
  $extraPaths = @([regex]::Matches($helpRaw, '/lol-(tencent-[a-z\-]*match[a-z\-]*|end-of-game|replays[^/]*)/[a-zA-Z0-9_/\-\.\{\}\?=&]*') | ForEach-Object { $_.Value } | Sort-Object -Unique | Select-Object -First 5)
}
Write-Host ("    /Help 共 {0} 字节，match-history 路由 {1} 条" -f $helpRaw.Length, $mhPaths.Count)
foreach ($p in $mhPaths) { Write-Host ("      $p") }
if ($extraPaths.Count -gt 0) {
  Write-Host '    其他战绩相关路由：'
  foreach ($p in $extraPaths) { Write-Host ("      $p") }
}
Write-Host ''

# [6] 逐条实测无参/可替换参数的路由
Write-Host '[6] 路由实测（占位符替换为当前账号）：'
$cands = @()
foreach ($p in $mhPaths) {
  $real = $p -replace '\{accountId\}', $accountId -replace '\{puuid\}', $puuid -replace '\{summonerId\}', $puuid
  if ($real -notmatch '\{') { $cands += $real }
}
$hit = $null
$tested = 0
foreach ($u in ($cands | Select-Object -First 12)) {
  $tested++
  $full = "https://127.0.0.1:$port" + ($u -replace '\?.*$', '?begIndex=0&endIndex=20' ) 
  if ($u -notmatch '\?') { $full = "https://127.0.0.1:$port$u`?begIndex=0&endIndex=20" }
  $tf2 = Join-Path $env:TEMP 'lcu_try10.json'
  if (Test-Path $tf2) { Remove-Item $tf2 -Force }
  $code2 = (& curl.exe -k -s -m 10 -H "Authorization: Basic $b64" -o $tf2 -w '%{http_code}' $full 2>$null | Select-Object -Last 1)
  if (-not $code2) { $code2 = 'ERR' }
  $b2 = ''
  if ((Test-Path $tf2) -and ((Get-Item $tf2).Length -gt 0)) { $b2 = [IO.File]::ReadAllText($tf2) }
  $m2 = ''
  if ($b2 -match 'gameId' -and $b2 -notmatch 'errorCode') { $m2 = '  <- ★ 有数据'; if (-not $hit) { $hit = @{ Url = $u; Body = $b2 } } }
  elseif ($b2 -match 'errorCode') { $m2 = '  <- 错误' }
  Write-Host ("    HTTP={0,-3} {1}{2}" -f $code2, $u, $m2)
}
Write-Host ''

# [7] 汇总结论
Write-Host '================ 诊断结论 ================' -ForegroundColor Cyan
Write-Host ("  客户端战绩页(UI): {0}" -f $uiState)
Write-Host ("  LCU 战绩接口(脚本走的): 重试 3 次均未取到数据" )
if ($hit) {
  Write-Host ("  发现可用的替代路由: {0}" -f $hit.Url) -ForegroundColor Green
  [IO.File]::WriteAllText((Join-Path $desk 'lol_alt_route10.json'), $hit.Body, (New-Object Text.UTF8Encoding $false))
  Write-Host '  已把它返回的数据存到桌面 lol_alt_route10.json，请连同窗口截图发回，我来改同步脚本适配。'
} elseif (-not $uiOk) {
  Write-Host '  判断：客户端 UI 自己也拉不到战绩 —— 是腾讯战绩服务端对您账号暂时不可用（抽风/维护）。'
  Write-Host '  建议：1) 托盘右键完全退出 LOL 再重开试一次  2) 仍不行就等几小时或明天再同步，数据不会丢。'
} else {
  Write-Host '  判断：客户端 UI 能显示战绩，但 LCU 接口 503 —— 属于接口层未就绪。'
  Write-Host '  建议：托盘右键完全退出 LOL，重新打开客户端并登录，等待 2 分钟后再跑一次本诊断。'
  Write-Host '        若重启后依旧（UI 能看、接口 503），请把本窗口截图发回，我按新路由改同步脚本。'
}
Write-Host '==========================================' -ForegroundColor Cyan
Write-Host ''
Write-Host '诊断完成：请把本窗口完整截图发回（有提示存桌面文件时请一并传回）。'
Read-Host '按回车键关闭'
