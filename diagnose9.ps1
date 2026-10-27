# ============================================================
#  LOL LCU 诊断9：战绩列表为空 专项排查
#  只读诊断，不改任何数据。请【右键 diagnose9.bat -> 以管理员身份运行】
#  前提：LOL 客户端开着并已登录
# ============================================================

$ErrorActionPreference = 'Continue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

Write-Host ''
Write-Host '======== LOL LCU 诊断9：战绩列表为空专项 ========'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host '[!] 当前非管理员权限，读取客户端命令行可能失败，建议关闭后右键以管理员身份运行' -ForegroundColor Yellow }
Write-Host ''

# [1] 提取端口与令牌
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
Write-Host "[1] LCU 端口=$port 令牌已取得"
Write-Host ''

# [2] 当前登录账号（判断是否登错号）
$t0 = Join-Path $env:TEMP 'lcu_summoner9.json'
if (Test-Path $t0) { Remove-Item $t0 -Force }
$code0 = (& curl.exe -k -s -m 12 -H "Authorization: Basic $b64" -o $t0 -w '%{http_code}' "https://127.0.0.1:$port/lol-summoner/v1/current-summoner" 2>$null | Select-Object -Last 1)
$s0 = ''
if ((Test-Path $t0) -and ((Get-Item $t0).Length -gt 0)) { $s0 = [IO.File]::ReadAllText($t0) }
$accountName = ''; $puuid = ''; $accountId = ''
if ($s0 -and ($s0 -notmatch 'errorCode')) {
  $sj = $null
  try { $sj = $s0 | ConvertFrom-Json } catch {}
  if ($sj) {
    $accountName = [string]$sj.displayName
    if (-not $accountName) { $accountName = [string]$sj.gameName }   # 国服 Riot ID：displayName 为空，真名在 gameName
    $puuid = [string]$sj.puuid
    $accountId = [string]$sj.accountId
    Write-Host ("[2] 当前登录账号: {0}   等级={1}" -f $accountName, $sj.summonerLevel)
    Write-Host ("    puuid={0}" -f $puuid)
    Write-Host ("    accountId={0}" -f $accountId)
  }
}
if (-not $accountName) {
  Write-Host ("[2] 账号信息拉取失败 HTTP={0}，响应开头：" -f $code0)
  if ($s0) { Write-Host ('    ' + $s0.Substring(0, [Math]::Min(200, $s0.Length))) }
}
Write-Host ''
if ($accountName) {
  Write-Host '    >>> 请核对上面显示的就是打自定义对局的那个账号（登错号列表必然为空）'
  Write-Host ''
}

# [3] 战绩接口探测矩阵（打印 HTTP 状态码 + 响应开头，覆盖多种端点/参数形态）
$cands = @()
$cands += @{ Name = 'A current-summoner + begIndex'; Url = "https://127.0.0.1:$port/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=20" }
$cands += @{ Name = 'B current-summoner 无参数';     Url = "https://127.0.0.1:$port/lol-match-history/v1/products/lol/current-summoner/matches" }
if ($accountId) { $cands += @{ Name = 'C accountId + begIndex';  Url = "https://127.0.0.1:$port/lol-match-history/v1/products/lol/$accountId/matches?begIndex=0&endIndex=20" } }
if ($puuid)     { $cands += @{ Name = 'D puuid + begIndex';      Url = "https://127.0.0.1:$port/lol-match-history/v1/products/lol/$puuid/matches?begIndex=0&endIndex=20" } }
$cands += @{ Name = 'E history-lite + begIndex'; Url = "https://127.0.0.1:$port/lol-match-history/v1/history-lite?begIndex=0&endIndex=20" }
$cands += @{ Name = 'F history-lite + count';    Url = "https://127.0.0.1:$port/lol-match-history/v1/history-lite?count=20" }

$hit = $null
Write-Host '[3] 战绩接口探测（HTTP状态 / 内容开头）：'
foreach ($c in $cands) {
  $tf = Join-Path $env:TEMP 'lcu_probe9.json'
  if (Test-Path $tf) { Remove-Item $tf -Force }
  $code = (& curl.exe -k -s -m 12 -H "Authorization: Basic $b64" -o $tf -w '%{http_code}' $c.Url 2>$null | Select-Object -Last 1)
  if (-not $code) { $code = 'ERR' }
  $body = ''
  if ((Test-Path $tf) -and ((Get-Item $tf).Length -gt 0)) { $body = [IO.File]::ReadAllText($tf) }
  $mark = ''
  if ($body -match 'errorCode') { $mark = '  <- 错误响应' }
  elseif ($code -eq '200' -and $body -match 'gameId') { $mark = '  <- ★ 有对局数据' }
  elseif ($code -ne '200') { $mark = '  <- 非200状态' }
  elseif ($body -match '"games":\s*\[\s*\]') { $mark = '  <- 空列表' }
  $head = $body
  if ($head.Length -gt 200) { $head = $head.Substring(0, 200) + '...' }
  Write-Host ("    [{0}]  HTTP={1}  {2}字符{3}" -f $c.Name, $code, $body.Length, $mark)
  if ($head) { Write-Host ("      $head") }
  if (-not $hit -and ($code -eq '200') -and ($body -match 'gameId') -and ($body -notmatch 'errorCode')) {
    $hit = @{ Name = $c.Name; Body = $body }
  }
}
Write-Host ''

# [4] 命中解析：类型统计 + 时间窗
if (-not $hit) {
  Write-Host '[X] 全部端点都没有取到对局数据——常见原因（按可能性排序）：'
  Write-Host '    1) 登录的不是打自定义的那个号：看上面 [2] 显示的账号名'
  Write-Host '    2) 客户端刚启动战绩还没加载：在 LOL 客户端里点开【生涯】-【战绩记录】页，'
  Write-Host '       等它显示出历史对局列表后，再重跑一次本诊断'
  Write-Host '    3) LOL 客户端版本更新改了接口：请把本窗口完整截图发回给开发者'
  Read-Host '按回车键关闭'
  exit 1
}
Write-Host ("[OK] 命中端点: {0}" -f $hit.Name)
[IO.File]::WriteAllText((Join-Path $desk 'lol_list_raw9.json'), $hit.Body, (New-Object Text.UTF8Encoding $false))
$lj = $null
try { $lj = $hit.Body | ConvertFrom-Json } catch {}
$games = $null
if ($lj) {
  if ($lj.games -and $lj.games.games) { $games = @($lj.games.games) }
  elseif ($lj.games) { $games = @($lj.games) }
  elseif ($lj -is [array]) { $games = @($lj) }
}
if (-not $games -or $games.Count -eq 0) {
  Write-Host '[!] 响应里有 gameId 但数组解析失败——桌面 lol_list_raw9.json 请传回给开发者'
  Read-Host '按回车键关闭'
  exit 1
}
$epoch = [DateTime]'1970-01-01'
$cutoffMs = (([DateTime]::UtcNow).AddDays(-6) - $epoch).TotalMilliseconds
Write-Host ''
Write-Host ("[4] 共 {0} 条原始记录，按对局类型统计：" -f $games.Count)
foreach ($grp in ($games | Group-Object gameType)) {
  Write-Host ("      {0}  x{1}" -f $grp.Name, $grp.Count)
}
$recent = @($games | Where-Object { [double]$_.gameCreation -ge $cutoffMs })
$recentCustom = @($recent | Where-Object { $_.gameType -eq 'CUSTOM_GAME' })
Write-Host ''
Write-Host ("    最近 6 天内: {0} 局，其中自定义(CUSTOM_GAME): {1} 局" -f $recent.Count, $recentCustom.Count)
Write-Host ("    同步时间窗起点: {0}" -f $epoch.AddMilliseconds($cutoffMs).ToLocalTime().ToString('MM-dd HH:mm'))
Write-Host ''
Write-Host '    最新 5 局：'
foreach ($g in ($recent | Select-Object -First 5)) {
  $tg = $epoch.AddMilliseconds([double]$g.gameCreation).ToLocalTime().ToString('MM-dd HH:mm')
  Write-Host ("      gameId={0}  类型={1}  时间={2}" -f $g.gameId, $g.gameType, $tg)
}
Write-Host ''
Write-Host '诊断完成：请把本窗口完整截图发回（若上方有提示，请把桌面 lol_list_raw9.json 一并发回）。'
Read-Host '按回车键关闭'
