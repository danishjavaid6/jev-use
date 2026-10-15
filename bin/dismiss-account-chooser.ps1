param([Parameter(Mandatory=$true)][int]$DebugPort, [switch]$Once, [switch]$Inspect)
$ErrorActionPreference = 'Stop'
# UI Automation invokes a named button without focusing the window or sending keys.
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
try {
    $owners = @(Get-NetTCPConnection -LocalPort $DebugPort -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -ne 1) { throw 'Cannot identify one browser process for this debugging port' }
    $browserPid = [int]$owners[0]
    $pidCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $browserPid)
    # Chrome/GoLogin has used several titles for the browser-owned profile
    # chooser. Match only those known credential/profile dialogs.
    $headingConditions = @('Sign in as', 'Choose a profile', "Who's using Chrome?", 'Choose an account') | ForEach-Object {
        New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $_)
    }
    $headingCondition = New-Object System.Windows.Automation.OrCondition(,$headingConditions)
    $closeConditions = @('Close', 'Cancel') | ForEach-Object {
        New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $_)
    }
    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
    $closeNameCondition = New-Object System.Windows.Automation.OrCondition(,$closeConditions)
    $closeCondition = New-Object System.Windows.Automation.AndCondition($closeNameCondition, $buttonCondition)
    $notificationTextCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Show notifications')
    $modalCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.WindowPatternIdentifiers]::IsModalProperty, $true)
    $blockNameCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Block')
    $blockCondition = New-Object System.Windows.Automation.AndCondition($blockNameCondition, $buttonCondition)
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    function Test-WebContentElement($element) {
        # HTML aria-modal dialogs can expose UIA WindowPattern.IsModal too.
        # They belong to the web document and are handled by the page workflow.
        for ($node = $element; $null -ne $node; $node = $walker.GetParent($node)) {
            if ($node.Current.ControlType -eq [System.Windows.Automation.ControlType]::Document) { return $true }
            if ($node.Current.ClassName -eq 'Chrome_WidgetWin_1') { break }
        }
        return $false
    }
    $readyReported = $false
    $stalePasses = 0
    while ($true) {
      try {
        # Chromium exposes both a RootView and a Label with the same heading.
        # Several ancestor searches can resolve to the very same Close control.
        # Count unique UIA elements, not heading/search matches.
        $candidates = @{}
        $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCondition)
        foreach ($window in $windows) {
            $headings = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $headingCondition)
            foreach ($heading in $headings) {
                # BrowserRootView repeats the selected tab title. A tab named
                # 'Sign in as' is not a modal and only offers window/tab Close.
                if ($heading.Current.ClassName -eq 'BrowserRootView' -or (Test-WebContentElement $heading)) { continue }
                if ($Inspect) {
                    $inspectNode = $heading
                    $ancestry = @()
                    for ($inspectDepth = 0; $inspectDepth -lt 12 -and $null -ne $inspectNode; $inspectDepth++) {
                        $inspectButtons = $inspectNode.FindAll([System.Windows.Automation.TreeScope]::Descendants, $closeCondition)
                        $ancestry += @{depth=$inspectDepth; className=$inspectNode.Current.ClassName; closeCount=$inspectButtons.Count; offscreen=$inspectNode.Current.IsOffscreen; webContent=(Test-WebContentElement $inspectNode)}
                        if ($inspectNode.Equals($window)) { break }
                        $inspectNode = $walker.GetParent($inspectNode)
                    }
                    [Console]::Out.WriteLine((@{heading=$heading.Current.Name; ancestors=$ancestry} | ConvertTo-Json -Depth 5 -Compress))
                    $inspectCloseDetails = @()
                    foreach ($inspectClose in $heading.FindAll([System.Windows.Automation.TreeScope]::Descendants, $closeCondition)) {
                        $inspectParents = @()
                        $inspectParent = $walker.GetParent($inspectClose)
                        for ($parentDepth=0; $parentDepth -lt 5 -and $null -ne $inspectParent; $parentDepth++) {
                            $inspectParents += @{className=$inspectParent.Current.ClassName; controlType=$inspectParent.Current.ControlType.ProgrammaticName}
                            $inspectParent=$walker.GetParent($inspectParent)
                        }
                        $inspectCloseDetails += @{name=$inspectClose.Current.Name; className=$inspectClose.Current.ClassName; runtimeId=($inspectClose.GetRuntimeId() -join ':'); offscreen=$inspectClose.Current.IsOffscreen; enabled=$inspectClose.Current.IsEnabled; webContent=(Test-WebContentElement $inspectClose); parents=$inspectParents}
                    }
                    [Console]::Out.WriteLine((@{closeControls=$inspectCloseDetails} | ConvertTo-Json -Depth 7 -Compress))
                }
                # The chooser's Close button is nested several levels below
                # the heading's parent in Orbita/Chrome. Start at the heading
                # itself and allow a few extra ancestor levels.
                $node = $heading
                # Do not climb to the whole browser window and invoke an unrelated Close.
                for ($depth = 0; $depth -lt 8 -and $null -ne $node; $depth++) {
                    if ($node.Equals($window)) { break }
                    $buttons = $node.FindAll([System.Windows.Automation.TreeScope]::Descendants, $closeCondition)
                    if ($buttons.Count -eq 1 -and $buttons[0].Current.ClassName -notin @('WindowsCaptionButton','TabCloseButton') -and -not (Test-WebContentElement $buttons[0]) -and -not $buttons[0].Current.IsOffscreen -and $buttons[0].Current.IsEnabled) {
                        $pattern = $null
                        if ($buttons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                            $runtimeKey = $buttons[0].GetRuntimeId() -join ':'
                            $candidates[$runtimeKey] = $buttons[0]
                            break
                        }
                    }
                    $node = $walker.GetParent($node)
                }
            }
            $notification = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $notificationTextCondition)
            foreach ($prompt in $notification) {
                $node = $prompt
                for ($depth = 0; $depth -lt 8 -and $null -ne $node; $depth++) {
                    if ($node.Equals($window)) { break }
                    $buttons = $node.FindAll([System.Windows.Automation.TreeScope]::Descendants, $blockCondition)
                    if ($buttons.Count -eq 1 -and -not $buttons[0].Current.IsOffscreen -and $buttons[0].Current.IsEnabled) {
                        $pattern = $null
                        if ($buttons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                            $pattern.Invoke()
                            [Console]::Out.WriteLine('{"dismissed":"Windows.NotificationPermission"}')
                            break
                        }
                    }
                    $node = $walker.GetParent($node)
                }
            }
        }
        if ($candidates.Count -eq 1) {
            $candidate = @($candidates.Values)[0]
            $invoke = $candidate.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $invoke.Invoke()
            $closed = $false
            for ($attempt = 0; $attempt -lt 10; $attempt++) {
                Start-Sleep -Milliseconds 100
                try {
                    $remaining = 0
                    foreach ($window in $windows) {
                        foreach ($remainingHeading in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $headingCondition)) {
                            if ($remainingHeading.Current.ClassName -ne 'BrowserRootView' -and -not $remainingHeading.Current.IsOffscreen -and -not (Test-WebContentElement $remainingHeading)) { $remaining++ }
                        }
                    }
                    if ($remaining -eq 0) { $closed = $true; break }
                } catch [System.Windows.Automation.ElementNotAvailableException] { $closed = $true; break }
            }
            if (-not $closed) { throw 'Close was invoked but the account chooser remained visible' }
            [Console]::Out.WriteLine('{"dismissed":"Windows.ProfileChooser"}')
        }
        # Publish a fresh, non-secret browser-chrome inspection every pass.
        # Unknown modal dialogs are reported, never closed with an arbitrary button.
        $blockedKinds = @()
        $freshWindows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCondition)
        foreach ($window in $freshWindows) {
            foreach ($heading in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $headingCondition)) {
                if ($heading.Current.ClassName -eq 'BrowserRootView' -or (Test-WebContentElement $heading)) { continue }
                if (-not $heading.Current.IsOffscreen) { $blockedKinds += 'account_chooser'; break }
            }
            foreach ($prompt in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $notificationTextCondition)) {
                if (-not $prompt.Current.IsOffscreen) { $blockedKinds += 'notification_prompt'; break }
            }
            foreach ($modal in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $modalCondition)) {
                if (-not $modal.Current.IsOffscreen -and -not (Test-WebContentElement $modal)) { $blockedKinds += 'native_modal'; break }
            }
        }
        $blockedKinds = @($blockedKinds | Select-Object -Unique)
        [Console]::Out.WriteLine((@{inspected=$true; blocked=($blockedKinds.Count -gt 0); kinds=$blockedKinds} | ConvertTo-Json -Compress))
        if (-not $readyReported) {
            [Console]::Out.WriteLine('{"ready":true}')
            $readyReported = $true
        }
        $stalePasses = 0
        if ($Once) { break }
        Start-Sleep -Milliseconds 1000
      } catch {
        $baseException = $_.Exception.GetBaseException()
        if ($baseException -is [System.Windows.Automation.ElementNotAvailableException] -and $stalePasses -lt 3) {
          $stalePasses++
          # A dismissed prompt can invalidate UIA nodes mid-pass. No clear
          # result is published until a complete fresh inspection succeeds.
          Start-Sleep -Milliseconds 250
          continue
        }
        throw
      }
    }
} catch {
    [Console]::Out.WriteLine((@{exceptionType=$_.Exception.GetBaseException().GetType().FullName} | ConvertTo-Json -Compress))
    [Console]::Out.WriteLine('{"warning":"Windows account chooser dismissal unavailable; inspect the popup before continuing"}')
    exit 1
}
