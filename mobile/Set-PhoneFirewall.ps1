param(
    [ValidateSet('Preview', 'Enable', 'Remove')][string]$Action = 'Preview',
    [Parameter(Mandatory = $true)][string]$LocalAddress,
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [Parameter(Mandatory = $true)][string]$PythonPath
)
$ErrorActionPreference = 'Stop'
$ip = [System.Net.IPAddress]::Parse($LocalAddress)
$bytes = $ip.GetAddressBytes()
if ($bytes.Length -ne 4) { throw 'IPv4 only.' }
$isLan = ($bytes[0] -eq 10) -or ($bytes[0] -eq 192 -and $bytes[1] -eq 168) -or
    ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31)
$isMesh = $bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127
if (-not ($isLan -or $isMesh)) { throw 'Use the actual LAN or private mesh IPv4 address.' }
if (-not (Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq $LocalAddress)) {
    throw 'That address is not assigned to this computer.'
}
$program = (Resolve-Path -LiteralPath $PythonPath).Path
if ((Split-Path -Leaf $program) -notin @('python.exe', 'pythonw.exe')) {
    throw 'Select the Python executable used to start Context Relay.'
}
$hasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $hash = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($program.ToLowerInvariant()))).Replace('-', '').Substring(0, 12)
} finally { $hasher.Dispose() }
$name = 'ContextRelay.Phone.' + $LocalAddress.Replace('.', '_') + '.' + $Port + '.' + $hash
$description = 'Context Relay mobile gateway v1; explicit private-network access only'
$remoteAddress = if ($isMesh) { '100.64.0.0/10' } else { 'LocalSubnet' }
$values = @{
    Name = $name; DisplayName = $name; Description = $description;
    Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'; Profile = 'Any';
    Protocol = 'TCP'; LocalPort = $Port; LocalAddress = $LocalAddress;
    RemoteAddress = $remoteAddress; Program = $program; EdgeTraversalPolicy = 'Block'
}
if ($Action -eq 'Preview') {
    [pscustomobject]$values | ConvertTo-Json
    exit 0
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Preview is available without elevation. Run this script as administrator to enable or remove this one scoped rule.'
}
$existing = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($existing) {
    $application = $existing | Get-NetFirewallApplicationFilter
    $ports = $existing | Get-NetFirewallPortFilter
    $addresses = $existing | Get-NetFirewallAddressFilter
    if (@($existing).Count -ne 1 -or $existing.Description -ne $description -or
        $existing.Direction -ne 'Inbound' -or $existing.Action -ne 'Allow' -or
        $existing.Enabled -ne 'True' -or $existing.Profile -ne 'Any' -or
        $existing.EdgeTraversalPolicy -ne 'Block' -or $ports.Protocol -notin @('TCP', '6') -or
        $application.Program -ne $program -or $ports.LocalPort -ne $Port -or
        @($addresses.LocalAddress).Count -ne 1 -or $addresses.LocalAddress -ne $LocalAddress -or
        @($addresses.RemoteAddress).Count -ne 1 -or $addresses.RemoteAddress -ne $remoteAddress) {
        throw 'A different or edited firewall rule already uses this name. Nothing was changed.'
    }
}
if ($Action -eq 'Remove') {
    if ($existing) { $existing | Remove-NetFirewallRule }
    Write-Output 'The owned Context Relay rule is absent.'
} elseif ($existing) {
    Write-Output 'The matching rule already exists; no change was made.'
} else {
    New-NetFirewallRule @values | Select-Object Name, Enabled, Direction, Action
}
