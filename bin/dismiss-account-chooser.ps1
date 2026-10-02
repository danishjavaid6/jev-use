param([Parameter(Mandatory=$true)][int]$DebugPort)
$ErrorActionPreference = 'Stop'
# UI Automation invokes a named button without focusing the window or sending keys.
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
try {
    $owners = @(Get-NetTCPConnection -LocalPort $DebugPort -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -ne 1) { throw 'Cannot identify one browser process for this debugging port' }
    $browserPid = [int]$owners[0]
    $pidCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $browserPid)
    $headingCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Sign in as')
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Close')
    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
    $closeCondition = New-Object System.Windows.Automation.AndCondition($nameCondition, $buttonCondition)
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    while ($true) {
        $candidates = @()
        $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCondition)
        foreach ($window in $windows) {
            $headings = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $headingCondition)
            foreach ($heading in $headings) {
                $node = $walker.GetParent($heading)
                # Do not climb to the whole browser window and invoke an unrelated Close.
                for ($depth = 0; $depth -lt 5 -and $null -ne $node; $depth++) {
                    if ($node.Equals($window)) { break }
                    $buttons = $node.FindAll([System.Windows.Automation.TreeScope]::Descendants, $closeCondition)
                    if ($buttons.Count -eq 1 -and -not $buttons[0].Current.IsOffscreen -and $buttons[0].Current.IsEnabled) {
                        $pattern = $null
                        if ($buttons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                            $candidates += $buttons[0]
                            break
                        }
                    }
                    $node = $walker.GetParent($node)
                }
            }
        }
        if ($candidates.Count -eq 1) {
            $invoke = $candidates[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $invoke.Invoke()
            $closed = $false
            for ($attempt = 0; $attempt -lt 10; $attempt++) {
                Start-Sleep -Milliseconds 100
                try {
                    $remaining = @()
                    foreach ($window in $windows) {
                        $remaining += @($window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $headingCondition))
                    }
                    if ($remaining.Count -eq 0) { $closed = $true; break }
                } catch [System.Windows.Automation.ElementNotAvailableException] { $closed = $true; break }
            }
            if (-not $closed) { throw 'Close was invoked but the account chooser remained visible' }
            [Console]::Out.WriteLine('{"dismissed":"Windows.SignInAs"}')
        }
        Start-Sleep -Milliseconds 1000
    }
} catch {
    [Console]::Out.WriteLine('{"warning":"Windows account chooser dismissal unavailable; inspect the popup before continuing"}')
    exit 1
}
