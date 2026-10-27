param(
  [switch]$NoElevate,   # 测试用：跳过自动提权
  [ValidateSet('Interactive','Preview','Sync')]
  [string]$Mode = 'Interactive',
  [string]$OutputFile = ''
)

# ============================================================
#  LOL 自定义对局 → 抽签积分系统 同步工具（数据源电脑专用·最终版）
#  用法：与 lol_sync.bat 放同一文件夹，双击 lol_sync.bat 即可
#  - 自动申请管理员权限（读取 LCU 令牌需要）
#  - LCU 端点为国服实测路径，参数自动探测
# ============================================================

# ------------------ 自动提权 ------------------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $NoElevate -and -not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host '需要管理员权限（读取 LOL 客户端令牌），正在申请...请在弹窗中点【是】'
  $argLine = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
  if ($Mode -ne 'Interactive') { $argLine += ' -Mode ' + $Mode + ' -OutputFile "' + $OutputFile + '"' }
  try {
    $elevated = Start-Process powershell.exe -Verb RunAs -ArgumentList $argLine -Wait -PassThru
    exit $elevated.ExitCode
  } catch {
    if ($OutputFile) {
      @{ ok = $false; error = '用户取消了管理员权限请求' } | ConvertTo-Json -Compress | Set-Content -Path $OutputFile -Encoding UTF8
    }
    exit 1
  }
}

# ------------------ 配置区 ------------------
$ServerUrl = 'https://124777lk2jf53.vicp.fun'   # 服务器地址（花生壳固定域名，已正式开通至2099，永久不变）
$Days      = 6                                    # 只同步最近 N 天的自定义对局（含当天，可覆盖到前几天全天）
# --------------------------------------------

$ErrorActionPreference = 'Continue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

function Write-JsonResult($data) {
  if ($OutputFile) {
    $json = ConvertTo-Json -InputObject $data -Depth 10 -Compress
    Set-Content -Path $OutputFile -Value $json -Encoding UTF8
  }
}

Write-Host ''
Write-Host '======== LOL 战绩 → 抽签系统 同步工具 ========'
Write-Host ''

# ------------------ [1] 提取 LCU 端口与令牌 ------------------
$ux = Get-CimInstance Win32_Process -Filter "Name='LeagueClientUx.exe'" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ux -or -not $ux.CommandLine) {
  Write-Host '[X] 读不到 LOL 客户端信息！' -ForegroundColor Red
  Write-Host '    请确认：1) LOL 客户端已启动并登录  2) 本窗口标题栏有"管理员"字样'
  if ($Mode -ne 'Interactive') { Write-JsonResult @{ ok = $false; error = '未检测到已启动并登录的 LOL 客户端' }; exit 1 }
  Read-Host '按回车键关闭'
  exit 1
}
$port = $null; $token = $null
if ($ux.CommandLine -match '--app-port=(\d+)') { $port = $Matches[1] }
if ($ux.CommandLine -match '--remoting-auth-token=([A-Za-z0-9_:=+-]+)') { $token = $Matches[1] }
if (-not $port -or -not $token) {
  Write-Host '[X] 未能从客户端命令行解析端口/令牌（客户端状态异常，可重启客户端后再试）' -ForegroundColor Red
  if ($Mode -ne 'Interactive') { Write-JsonResult @{ ok = $false; error = '无法读取 LOL 客户端连接信息，请重启客户端后重试' }; exit 1 }
  Read-Host '按回车键关闭'
  exit 1
}
$b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("riot:$token"))
$desk = [Environment]::GetFolderPath('Desktop')
Write-Host "[OK] 已连接 LOL 客户端 (LCU 端口 $port)"
Write-Host ''

# ------------------ [1.5] 英雄对照表（国服中文名，随客户端版本自动更新） ------------------
$ChampMap = @{}
$ct = Join-Path $env:TEMP 'lcu_champ.json'
if (Test-Path $ct) { Remove-Item $ct -Force }
& curl.exe -k -s -m 15 -H "Authorization: Basic $b64" -o $ct "https://127.0.0.1:$port/lol-game-data/assets/v1/champion-summary.json" 2>$null
if ((Test-Path $ct) -and ((Get-Item $ct).Length -gt 0)) {
  try {
    $cjson = [IO.File]::ReadAllText($ct) | ConvertFrom-Json
    foreach ($c in @($cjson)) { if ($c.id -gt 0 -and $c.name) { $ChampMap[[int]$c.id] = [string]$c.name } }
  } catch {}
}
if ($ChampMap.Count -gt 0) { Write-Host "[OK] 英雄对照表已加载（$($ChampMap.Count) 个英雄）" }
else { Write-Host '[!] 英雄对照表拉取失败，本次不带英雄名（不影响同步）' -ForegroundColor Yellow }
Write-Host ''

# ------------------ [2] 拉取对局列表（国服端点 + 参数自动探测 + 自动翻页）------------------
$epoch = [DateTime]'1970-01-01'
$cutoffMs = (([DateTime]::UtcNow).AddDays(-$Days) - $epoch).TotalMilliseconds
$base = "https://127.0.0.1:$port/lol-match-history/v1/products/lol/current-summoner/matches"
$PAGE_SIZE = 20

function Get-ListPage([string]$variant) {
  $t = Join-Path $env:TEMP 'lcu_list.json'
  if (Test-Path $t) { Remove-Item $t -Force }
  & curl.exe -k -s -m 15 -H "Authorization: Basic $b64" -o $t ($base + $variant) 2>$null
  $raw = ''
  if ((Test-Path $t) -and ((Get-Item $t).Length -gt 0)) { $raw = [IO.File]::ReadAllText($t) }
  $script:lastListRaw = $raw   # 保留最后一次响应，失败时用于区分错误类型
  if (-not $raw -or $raw -match 'errorCode') { return $null }
  $lj = $null
  try { $lj = $raw | ConvertFrom-Json } catch { return $null }
  if ($lj.games -and $lj.games.games) { return @($lj.games.games) }
  elseif ($lj.games) { return @($lj.games) }
  elseif ($lj -is [array]) { return @($lj) }
  return $null
}

# 探测可用的参数组合
$variants = @("?begIndex=0&endIndex=$PAGE_SIZE", '?begIndex=0', '', '?count=20', '?startIndex=0&count=20')
$hitVariant = $null
$games = $null
foreach ($v in $variants) {
  $games = Get-ListPage $v
  if ($games -and $games.Count -gt 0) { $hitVariant = $v; break }
}
if (-not $games -or $games.Count -eq 0) {
  Write-Host '[X] 最近战绩列表为空或拉取失败' -ForegroundColor Red
  if ($script:lastListRaw -match 'RPC_ERROR') {
    Write-Host ''
    Write-Host '    【客户端战绩服务未就绪】LOL 客户端内部拉不到战绩（RPC_ERROR/503），不是脚本或账号问题' -ForegroundColor Yellow
    Write-Host '    解决步骤（按顺序试，每步之后重跑本工具）：' -ForegroundColor Yellow
    Write-Host '    1) 在 LOL 客户端里点开【生涯】-【战绩记录】页，等历史对局列表显示出来'
    Write-Host '       （客户端自己刷过一次战绩，这个接口才会就绪）'
    Write-Host '    2) 若客户端战绩页也一片空白/转圈：完全退出 LOL（右下角托盘右键退出）后重开客户端登录'
    Write-Host '    3) 以上都不行：腾讯服务器临时抽风，过半小时到几小时再试'
  } else {
    Write-Host '    常见原因：1) 登录的不是打自定义的号  2) 客户端刚启动战绩还没加载（在客户端里点开【生涯】-【战绩】页）'
    Write-Host '    仍失败请把 diagnose9.bat 和 diagnose9.ps1 一起拷到本目录，右键 diagnose9.bat 以管理员身份运行，把窗口截图发回'
  }
  if ($Mode -ne 'Interactive') { Write-JsonResult @{ ok = $false; error = '未读取到最近 6 天的自定义对局；请在 LOL 客户端打开「生涯-战绩记录」后重试' }; exit 1 }
  Read-Host '按回车键关闭'
  exit 1
}
Write-Host "[OK] 列表接口连通（参数组合: $(if ($hitVariant) { $hitVariant } else { '无参数' })）"

# 自动翻页：begIndex 型组合可递增拉取，直到覆盖时间窗（最多 10 页）
if ($hitVariant -match 'begIndex=0') {
  $page = 1
  while ($page -lt 10) {
    $beg = $page * $PAGE_SIZE
    $v2 = "?begIndex=$beg&endIndex=$($beg + $PAGE_SIZE - 1)"
    $more = Get-ListPage $v2
    if (-not $more -or $more.Count -eq 0) { break }
    $games += $more
    $oldest = ($more | Measure-Object -Property gameCreation -Minimum).Minimum
    if ($oldest -lt $cutoffMs) { break }
    $page++
  }
  # 按 gameId 去重（翻页边界可能重叠）
  $games = @($games | Group-Object gameId | ForEach-Object { $_.Group[0] })
  Write-Host "[OK] 已翻页拉取 $($page) 页，共 $($games.Count) 条原始记录"
}

# 过滤：自定义对局 + 时间窗
$games = @($games | Where-Object { $_.gameType -eq 'CUSTOM_GAME' -and $_.gameCreation -ge $cutoffMs } | Sort-Object gameCreation -Descending)
if ($games.Count -eq 0) {
  Write-Host "[!] 最近 $Days 天内没有自定义对局（CUSTOM_GAME）战绩。"
  if ($Mode -ne 'Interactive') { Write-JsonResult @{ ok = $true; games = @(); message = "最近 $Days 天内没有自定义对局" }; exit 0 }
  Read-Host '按回车键关闭'
  exit 0
}
Write-Host "[OK] 找到 $($games.Count) 局自定义对局（最近 $Days 天）："
$i = 0
foreach ($g in $games) {
  $t = $epoch.AddMilliseconds([double]$g.gameCreation).ToLocalTime().ToString('MM-dd HH:mm')
  Write-Host ("  [{0}] {1}  ({2})" -f (++$i), $g.gameId, $t)
}
Write-Host ''

# ------------------ [3] 单局详情解析（名字走 gameName）------------------
function Get-LolDetail([long]$gid) {
  $t = Join-Path $env:TEMP 'lcu_detail.json'
  if (Test-Path $t) { Remove-Item $t -Force }
  & curl.exe -k -s -m 15 -H "Authorization: Basic $b64" -o $t "https://127.0.0.1:$port/lol-match-history/v1/games/$gid" 2>$null
  if (-not (Test-Path $t) -or (Get-Item $t).Length -eq 0) { return $null }
  $draw = [IO.File]::ReadAllText($t)
  $dj = $null
  try { $dj = $draw | ConvertFrom-Json } catch { return $null }
  if (-not ($dj -and $dj.participants)) { return $null }
  return $dj
}
function Convert-Participants($d) {
  # 名字在 participantIdentities[].player（国服 Riot ID：gameName 有值、summonerName 为空）
  $idmap = @{}
  foreach ($x in @($d.participantIdentities)) {
    $pl = $x.player
    if (-not $pl) { continue }
    $nm = $pl.gameName
    if (-not $nm) { $nm = $pl.summonerName }
    $idmap[[int]$x.participantId] = "$nm"
  }
  $out = @()
  foreach ($p in @($d.participants)) {
    $s = $p.stats
    # 多杀字段兼容：不同客户端/战绩服务版本返回的字段名可能不同
    # （quadraKills / QUADRA_KILLS / quadra_kills / QuadraKills 等），逐一探测避免漏记
    $__q = 0; $__p = 0
    if ($s) {
      foreach ($__n in @('quadraKills','QUADRA_KILLS','quadra_kills','QuadraKills')) { if ($null -ne $s.$__n) { $__q = [int]$s.$__n; break } }
      foreach ($__n in @('pentaKills','PENTA_KILLS','penta_kills','PentaKills'))  { if ($null -ne $s.$__n) { $__p = [int]$s.$__n; break } }
    }
    $out += [pscustomobject]@{
      name    = $idmap[[int]$p.participantId]
      team    = [int]$p.teamId
      win     = [bool]($s -and $s.win)
      kills   = if ($s) { [int]$s.kills } else { 0 }
      deaths  = if ($s) { [int]$s.deaths } else { 0 }
      assists = if ($s) { [int]$s.assists } else { 0 }
      dmg     = if ($s -and $null -ne $s.totalDamageDealtToChampions) { [int]$s.totalDamageDealtToChampions } else { 0 }
      gold    = if ($s -and $null -ne $s.goldEarned) { [int]$s.goldEarned } else { 0 }
      tk      = if ($s -and $null -ne $s.totalDamageTaken) { [int]$s.totalDamageTaken } else { 0 }
      heal    = if ($s -and $null -ne $s.totalHeal) { [int]$s.totalHeal } else { 0 }
      champ   = if ($ChampMap.Count -gt 0 -and $p.championId) { [string]$ChampMap[[int]$p.championId] } else { '' }
      quadraRaw = $__q
      penta   = $__p
      # 净化：五杀过程中必然伴生一次四杀播报，quadraKills 字段可能把五杀那次也计入；
      # 按最高档结算——净四杀 = 四杀字段 - 五杀次数（不为负），一次五杀绝不重复计成 四杀+五杀
      quadra  = if ($__q -gt $__p) { $__q - $__p } else { 0 }
    }
  }
  return $out
}

# ------------------ [4] 菜单 ------------------
Write-Host '请选择操作：'
Write-Host '  [1] 预览对局数据（核对名单/胜负/多杀识别，不入账）'
Write-Host '  [2] 同步到抽签系统（自动录入积分）'
Write-Host '  [0] 退出'
if ($Mode -eq 'Interactive') { $sel = Read-Host '输入编号后回车' }
elseif ($Mode -eq 'Preview') { $sel = '1' }
else { $sel = '2' }
Write-Host ''

if ($sel -eq '1') {
  # -------- 预览模式 --------
  $n = 0
  $sampleSaved = $false
  $detailFailures = 0
  $previewGames = @()
  foreach ($g in $games) {
    $n++
    $t = $epoch.AddMilliseconds([double]$g.gameCreation).ToLocalTime().ToString('MM-dd HH:mm')
    Write-Host ("===== 第 $n 局  $t (gameId=$($g.gameId)) =====")
    $d = Get-LolDetail $g.gameId
    if (-not $d) { $detailFailures++; Write-Host '  [!] 详情拉取失败' -ForegroundColor Red; continue }
    if (-not $sampleSaved) {
      $t3 = Join-Path $env:TEMP 'lcu_detail.json'
      if (Test-Path $t3) { [IO.File]::Copy($t3, (Join-Path $desk 'lol_sample.json'), $true) }
      $sampleSaved = $true
    }
    $ps = Convert-Participants $d
    $tA = @($ps | Where-Object { $_.team -le 100 } | Sort-Object win -Descending)
    $tB = @($ps | Where-Object { $_.team -gt 100 } | Sort-Object win -Descending)
    $previewGames += [pscustomobject]@{
      gameId = $g.gameId
      time = $t
      teamA = @($tA | ForEach-Object { @{ name = $_.name; win = $_.win; champ = $_.champ; quadra = $_.quadra; penta = $_.penta } })
      teamB = @($tB | ForEach-Object { @{ name = $_.name; win = $_.win; champ = $_.champ; quadra = $_.quadra; penta = $_.penta } })
    }
    $fmt = {
      param($x)
      $s = $x.name
      if ($x.win) { $s += '*胜' }
      $s += (' [' + $x.kills + '/' + $x.deaths + '/' + $x.assists + ']')
      if ($x.champ) { $s += (' <' + $x.champ + '>') }
      if ($x.dmg -gt 0) {
        $dw = [math]::Round($x.dmg / 10000, 1)
        $tw = [math]::Round($x.tk / 10000, 1)
        $hw = [math]::Round($x.heal / 10000, 1)
        $gw = [math]::Round($x.gold / 10000, 1)
        $s += (' [伤' + $dw + '万 承' + $tw + '万 治' + $hw + '万 金' + $gw + '万]')
      }
      if ($x.penta -gt 0)  { $s += (' [五杀x' + $x.penta + ']') }
      if ($x.quadra -gt 0) { $s += (' [四杀x' + $x.quadra + ']') }
      return $s
    }
    Write-Host ('  队伍A: ' + (($tA | ForEach-Object { & $fmt $_ }) -join '  '))
    Write-Host ('  队伍B: ' + (($tB | ForEach-Object { & $fmt $_ }) -join '  '))
    Write-Host ''
  }
  Write-Host '[提示] 名单正确的话，选 [2] 同步即可自动入账；桌面已存一局原始样本 lol_sample.json 可发开发者核对'
  if ($Mode -eq 'Preview') { Write-JsonResult @{ ok = $true; games = $previewGames; detailFailures = $detailFailures }; exit 0 }
}
elseif ($sel -eq '2') {
  # -------- 上报模式 --------
  Write-Host '正在拉取各局详情并推送服务器...'
  Write-Host ''
  $payload = @()
  $tq = 0; $tp = 0   # 本批识别到的四杀/五杀总数
  # 按「旧→新」顺序推送（服务器按推送顺序存战绩，保证战绩页最新局在最上面）
  foreach ($g in @($games | Sort-Object gameCreation)) {
    $d = Get-LolDetail $g.gameId
    if (-not $d) {
      Write-Host ("[!] gameId=$($g.gameId) 拉取详情失败，已跳过") -ForegroundColor Red
      continue
    }
    $ps = Convert-Participants $d
    foreach ($__x in $ps) { $tq += $__x.quadra; $tp += $__x.penta }   # 累加本批多杀总数
    $participants = @($ps | ForEach-Object {
      @{ name = $_.name; team = $_.team; win = $_.win
         kills = $_.kills; deaths = $_.deaths; assists = $_.assists
         dmg = $_.dmg; gold = $_.gold; tk = $_.tk; heal = $_.heal; champ = $_.champ
         multiKills = @{ quadra = $_.quadra; penta = $_.penta } }
    })
    $payload += @{
      gameId       = $g.gameId
      gameCreation = $g.gameCreation
      participants = $participants
    }
  }
  if ($payload.Count -eq 0) {
    Write-Host '没有可上报的对局。'
    if ($Mode -ne 'Interactive') { Write-JsonResult @{ ok = $false; error = '没有成功读取到可同步的对局详情' }; exit 1 }
    Read-Host '按回车键关闭'; exit 1
  }
  $body = ConvertTo-Json -InputObject @{ games = $payload } -Depth 8
  $bodyBytes = [Text.Encoding]::UTF8.GetBytes($body)
  try {
    $resp = Invoke-RestMethod -Uri "$ServerUrl/api/lol/import" -Method Post -Body $bodyBytes -ContentType 'application/json; charset=utf-8' -TimeoutSec 30
    Write-JsonResult $resp
    Write-Host ''
    Write-Host ("[完成] 新导入 $($resp.imported) 局" + $(if ($resp.imported -gt 0) { '，积分已自动更新！' } else { '（其余均已导入过或被跳过）' }))
    if (($tq + $tp) -gt 0) { Write-Host ("[多杀识别] 本批共识别到 四杀 $tq 次 · 五杀 $tp 次（已按规则计入）") }
    else { Write-Host '[多杀识别] 本批未识别到任何四杀/五杀——若实际有四/五，请把本窗口截图发回排查' -ForegroundColor Yellow }
    foreach ($r in @($resp.results)) {
      $line = "  gameId=$($r.gameId)  -> $($r.status)"
      if ($r.status -eq 'skipped') { $line += "  原因: $($r.reason)" }
      else { $line += "  胜方=$($r.result)  四杀/五杀标记=$($r.kills)" }
      Write-Host $line
    }
    Write-Host ''
    Write-Host '打开抽签系统即可看到最新战绩与积分：'
    Write-Host "  $ServerUrl"
  } catch {
    if ($Mode -ne 'Interactive') {
      Write-JsonResult @{ ok = $false; error = "推送服务器失败：$($_.Exception.Message)" }
      exit 1
    }
    Write-Host ''
    Write-Host '[X] 推送服务器失败！' -ForegroundColor Red
    Write-Host "    地址: $ServerUrl/api/lol/import"
    Write-Host "    错误: $($_.Exception.Message)"
    Write-Host ''
    Write-Host '    【最常见原因】cpolar 免费版地址已变（隧道重启/网络波动/服务器重启都会换地址，旧地址变 404）' -ForegroundColor Yellow
    Write-Host '    【解决办法】' -ForegroundColor Yellow
    Write-Host '    1. 在服务器电脑上双击「启动抽签.vbs」，弹窗会显示最新群分享链接（页面顶部也有）'
    Write-Host '    2. 打开本文件 lol_sync.ps1，把第 22 行的 $ServerUrl 改为该最新地址'
    Write-Host '    3. 重新双击 lol_sync.bat 即可：漏掉的对局会自动补传，已导入过的不会重复计分'
    Write-Host ''
    Write-Host '    其他可能：服务器电脑没开机 / 服务没在运行（在服务器电脑双击「启动抽签.vbs」恢复）'
  }
}
else { exit 0 }
if ($Mode -eq 'Interactive') { Read-Host '按回车键关闭' }
