# Evaluate the actual ownership predicate with synthetic rule data; no firewall changes.
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'Set-PhoneFirewall.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Firewall helper did not parse.' }
$condition = @($ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Clauses[0].Item1.Extent.Text.Contains('$existing.Description')
}, $true))
if ($condition.Count -ne 1) { throw 'Expected one ownership predicate.' }
$reject = [scriptblock]::Create($condition[0].Clauses[0].Item1.Extent.Text)
$description = 'Owned fixture rule'
$program = 'C:\Fixture\pythonw.exe'
$Port = 8765
$LocalAddress = '192.168.1.20'
$remoteAddress = 'LocalSubnet'
$existing = [pscustomobject]@{ Description = $description; Direction = 'Inbound'; Action = 'Allow';
    Enabled = 'True'; Profile = 'Any'; EdgeTraversalPolicy = 'Block' }
$application = [pscustomobject]@{ Program = $program }
$ports = [pscustomobject]@{ LocalPort = $Port; Protocol = 'TCP' }
$addresses = [pscustomobject]@{ LocalAddress = $LocalAddress; RemoteAddress = $remoteAddress }
if (& $reject) { throw 'Unchanged owned rule must match.' }
$mutations = @(
    @($existing, 'Description', 'foreign'), @($existing, 'Direction', 'Outbound'),
    @($existing, 'Action', 'Block'), @($existing, 'Enabled', 'False'),
    @($existing, 'Profile', 'Public'), @($existing, 'EdgeTraversalPolicy', 'Allow'),
    @($application, 'Program', 'C:\Other\pythonw.exe'), @($ports, 'Protocol', 'UDP'),
    @($ports, 'LocalPort', 1234), @($addresses, 'LocalAddress', '192.168.1.21'),
    @($addresses, 'RemoteAddress', 'Any')
)
foreach ($mutation in $mutations) {
    $object, $property, $replacement = $mutation
    $original = $object.$property
    $object.$property = $replacement
    if (-not (& $reject)) { throw "Edited $property must be rejected." }
    $object.$property = $original
}
$ports.Protocol = 6
if (& $reject) { throw 'Windows numeric TCP protocol must match.' }
Write-Output 'Firewall parser and 13 ownership checks passed; no rules changed.'
