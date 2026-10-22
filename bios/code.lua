-- PhoenixBIOS Setup Utility (OC Edition) - v8 (Multiline Error Fix)

local gpuAddr = component.list("gpu")()
if not gpuAddr then error("No GPU component!") end
local gpu = component.proxy(gpuAddr)
local screenAddr = component.list("screen")()
if screenAddr then gpu.bind(screenAddr, true) end
local W, H = gpu.getResolution()

-- ==== ЦВЕТА ====
local C_BG        = 0x000000
local C_TEXT      = 0xBBBBBB
local C_DIM       = 0x555555
local C_WHITE     = 0xFFFFFF
local C_BAR_BG    = 0x333333
local C_BAR_FG    = 0xCCCCCC
local C_ACTIVE_BG = 0x0000AA
local C_ACTIVE_FG = 0xFFFFFF
local C_SEL_BG    = 0x0000AA
local C_SEL_FG    = 0xFFFFFF
local C_WARN      = 0xFF5555
local C_FILL      = 0x00AA00
local C_EMPTY     = 0x333333
local C_MODAL_BG  = 0x111111

local function setCol(fg, bg) gpu.setForeground(fg); gpu.setBackground(bg) end

-- Очистка строки от ANSI и управляющих символов
local function cleanStr(s)
    if not s then return "" end
    s = tostring(s)
    s = s:gsub("\27%[[%d;]*m", "") -- Удаляем ANSI цвета
    s = s:gsub("[%z\1-\31]", " ")  -- Заменяем управляющие символы на пробелы
    return s
end

local function put(x, y, text, fg, bg)
    setCol(fg or C_TEXT, bg or C_BG)
    text = cleanStr(text)
    if #text > W - math.floor(x) + 1 then text = text:sub(1, W - math.floor(x) + 1) end
    gpu.set(math.floor(x), math.floor(y), text)
end

-- Специальная функция для вывода многострочного текста (ошибок)
local function putError(x, y, text, fg, bg)
    setCol(fg or C_TEXT, bg or C_BG)
    local lines = {}
    text = tostring(text)
    -- Сначала бьем на строки по \n
    for line in text:gmatch("[^\r\n]+") do
        -- Очищаем каждую строку от мусора
        line = line:gsub("\27%[[%d;]*m", "")
        line = line:gsub("[%z\1-\31]", " ")
        table.insert(lines, line)
    end
    -- Выводим построчно
    for i, line in ipairs(lines) do
        if y + i - 1 > H then break end
        if #line > W - math.floor(x) + 1 then
            line = line:sub(1, W - math.floor(x) + 1)
        end
        gpu.set(math.floor(x), math.floor(y) + i - 1, line)
    end
end

local function fill(x, y, w, h, ch, fg, bg)
    setCol(fg or C_TEXT, bg or C_BG)
    gpu.fill(math.floor(x), math.floor(y), math.floor(w), math.floor(h), ch or " ")
end
local function clear() fill(1, 1, W, H, " ", C_TEXT, C_BG) end

local function drawFrame(x, y, w, h, color)
    x, y, w, h = math.floor(x), math.floor(y), math.floor(w), math.floor(h)
    setCol(color, C_BG)
    gpu.set(x, y, "┌" .. string.rep("─", w - 2) .. "┐")
    gpu.set(x, y + h - 1, "└" .. string.rep("─", w - 2) .. "┘")
    for i = 1, h - 2 do
        gpu.set(x, y + i, "│")
        gpu.set(x + w - 1, y + i, "│")
    end
end

local function drawBar(x, y, w, percent)
    if percent < 0 then percent = 0 end
    if percent > 1 then percent = 1 end
    local n = math.floor(w * percent)
    setCol(C_FILL, C_FILL)
    if n > 0 then gpu.set(math.floor(x), math.floor(y), string.rep("█", n)) end
    setCol(C_EMPTY, C_EMPTY)
    if w - n > 0 then gpu.set(math.floor(x + n), math.floor(y), string.rep("░", w - n)) end
end

local function iterToList(it)
    if not it then return {} end
    if type(it) == "table" then return it end
    local t = {}
    for v in it do table.insert(t, v) end
    return t
end

local function fmtSize(b)
    if b >= 1048576 then return string.format("%.1f MB", b / 1048576) end
    if b >= 1024 then return string.format("%.1f KB", b / 1024) end
    return tostring(b) .. " B"
end
local function fmtTime(t) return string.format("%.2fs", t) end

local function fmtScore(n)
    n = math.floor(n)
    if n >= 1000000 then
        return string.format("%.1fM", n / 1000000)
    elseif n >= 10000 then
        return string.format("%.1fK", n / 1000)
    else
        return tostring(n)
    end
end

local function sleepPure(seconds)
    local deadline = computer.uptime() + seconds
    while true do
        local remain = deadline - computer.uptime()
        if remain <= 0 then break end
        computer.pullSignal(remain)
    end
end

local function readBootAddress()
    local ok, v = pcall(computer.getBootAddress)
    if ok and type(v) == "string" and v ~= "" then
        if component.proxy(v) then return v end
    end
    return nil
end

local function getSystemInfo()
    local info = {
        time = "00:00:00", date = "01/01/1970",
        cpu = "Unknown", memTotal = 0, memFree = 0,
        disks = {}, comps = {}, lua = _VERSION or "Unknown",
        uptime = 0, address = computer.address(),
        bootAddr = nil
    }
    local ok, v = pcall(os.date, "%H:%M:%S") if ok and v then info.time = v end
    ok, v = pcall(os.date, "%m/%d/%Y") if ok and v then info.date = v end
    ok, v = pcall(computer.getArchitecture) if ok and v then info.cpu = tostring(v) end
    ok, v = pcall(computer.totalMemory) if ok and v then info.memTotal = v end
    ok, v = pcall(computer.freeMemory) if ok and v then info.memFree = v end
    ok, v = pcall(computer.uptime) if ok and v then info.uptime = v end

    info.bootAddr = readBootAddress()

    for addr, ctype in component.list() do
        local c = { addr = addr, type = ctype, detail = "" }
        local proxy = component.proxy(addr)
        if ctype == "gpu" and proxy then
            local okM, m = pcall(proxy.maxResolution)
            if okM and m then c.detail = "Max: " .. tostring(m) end
        elseif ctype == "screen" and proxy then
            local okT, t = pcall(proxy.isTouchModeInverted)
            if okT then c.detail = t and "Touch Inverted" or "Normal" end
        elseif ctype == "filesystem" and proxy then
            local okL, l = pcall(proxy.getLabel)
            local okS, s = pcall(proxy.spaceTotal)
            local okU, u = pcall(proxy.spaceUsed)
            local label = okL and l or "Unnamed"
            local size = okS and s or 0
            local used = okU and u or 0
            c.detail = label .. " (" .. fmtSize(size) .. ")"
            table.insert(info.disks, {
                addr = addr, label = label,
                size = size, used = used,
                isDefaultBoot = (addr == info.bootAddr),
            })
        elseif ctype == "modem" and proxy then
            local okW, w = pcall(proxy.isWireless)
            c.detail = (okW and w) and "Wireless" or "Wired"
        end
        table.insert(info.comps, c)
    end
    table.sort(info.comps, function(a, b) return a.type < b.type end)
    return info
end

local function refreshDynamicInfo(info)
    local ok, v = pcall(os.date, "%H:%M:%S") if ok and v then info.time = v end
    ok, v = pcall(os.date, "%m/%d/%Y") if ok and v then info.date = v end
    ok, v = pcall(computer.freeMemory) if ok and v then info.memFree = v end
    ok, v = pcall(computer.uptime) if ok and v then info.uptime = v end
    for _, d in ipairs(info.disks) do
        local p = component.proxy(d.addr)
        if p then
            local okU, u = pcall(p.spaceUsed)
            if okU then d.used = u end
        end
    end
end

-- ==== ВЕРХНЯЯ ПАНЕЛЬ ВКЛАДОК ====
local TABS = {"Main", "Advanced", "System", "Hardware", "Boot", "Utilities", "Exit"}

local function drawTopBar(active)
    fill(1, 1, W, 1, " ", C_BAR_FG, C_BAR_BG)
    local x = 2
    for i, tab in ipairs(TABS) do
        local w = #tab + 2
        if i == active then
            fill(x, 1, w, 1, " ", C_ACTIVE_FG, C_ACTIVE_BG)
            put(x, 1, " " .. tab .. " ", C_ACTIVE_FG, C_ACTIVE_BG)
        else
            put(x, 1, " " .. tab .. " ", C_BAR_FG, C_BAR_BG)
        end
        x = x + w
    end
    local t = "OpenComputers Setup"
    if x + #t + 2 < W then
        put(W - #t - 1, 1, t, C_BAR_FG, C_BAR_BG)
    end
end

local function drawBottomBar()
    fill(1, H, W, 1, " ", C_BAR_FG, C_BAR_BG)
    put(2, H, "Esc Exit   Left/Right Tab   Up/Down Select   Enter Activate", C_BAR_FG, C_BAR_BG)
end

local function drawHelpPanel(text)
    local px = math.floor(W * 0.65)
    local pw = W - px
    if pw < 15 then return end
    fill(px + 1, 2, pw - 1, H - 3, " ", C_TEXT, C_BG)
    drawFrame(px + 1, 2, pw, H - 2, C_DIM)
    local title = " Item Specific Help "
    put(px + 1 + math.floor((pw - #title) / 2), 2, title, C_DIM, C_BG)
    if text then
        local words = {}
        for w in text:gmatch("%S+") do table.insert(words, w) end
        local line, ly = "", 4
        for _, w in ipairs(words) do
            if #line + #w + 1 > pw - 4 then
                put(px + 3, ly, line, C_TEXT, C_BG)
                ly = ly + 1
                line = w
                if ly > H - 2 then return end
            else
                line = line == "" and w or (line .. " " .. w)
            end
        end
        if line ~= "" and ly <= H - 2 then put(px + 3, ly, line, C_TEXT, C_BG) end
    end
end

local function drawRow(x, y, label, value, selected)
    local width = math.floor(W * 0.65) - x - 1
    if selected then
        fill(x, y, width, 1, " ", C_SEL_FG, C_SEL_BG)
        put(x + 1, y, label, C_SEL_FG, C_SEL_BG)
        if value then put(x + 28, y, value, C_SEL_FG, C_SEL_BG) end
    else
        put(x + 1, y, label, C_TEXT, C_BG)
        if value then put(x + 28, y, value, C_WHITE, C_BG) end
    end
end

local function diskLabel(d, i)
    return "Disk " .. i .. (d.isDefaultBoot and " *" or "")
end

-- ==== ВКЛАДКИ ====
local function drawMain(info, sel)
    local help, y = "", 3
    drawRow(1, y, "System Time:",      info.time, sel == 1); y = y + 1
    drawRow(1, y, "System Date:",      info.date, sel == 2); y = y + 1
    drawRow(1, y, "CPU Architecture:", info.cpu,  sel == 3); y = y + 1
    drawRow(1, y, "System Memory:",    fmtSize(info.memTotal), sel == 4); y = y + 1
    drawRow(1, y, "Extended Memory:",  fmtSize(info.memFree),  sel == 5); y = y + 2
    put(2, y, "Filesystems:", C_WHITE, C_BG); y = y + 1
    for i, d in ipairs(info.disks) do
        local lbl = diskLabel(d, i)
        local v = d.label .. " (" .. fmtSize(d.size) .. ")"
        if sel == 5 + i then help = "Address: " .. d.addr:sub(1, 8) end
        drawRow(2, y, lbl, v, sel == 5 + i)
        y = y + 1
    end
    drawHelpPanel(help)
end

local function drawAdvanced(_, sel)
    local y = 3
    drawRow(1, y, "Reboot System",   "", sel == 1); y = y + 1
    drawRow(1, y, "Shutdown System", "", sel == 2); y = y + 1
    drawHelpPanel("Press Enter to execute.")
end

local function drawSystemInfo(info)
    local y = 3
    put(2, y, "Runtime", C_WHITE, C_BG); y = y + 1
    put(2, y, "  Lua:", C_DIM, C_BG);        put(14, y, info.lua .. " / " .. info.cpu, C_TEXT, C_BG); y = y + 1
    local up = math.floor(info.uptime)
    local up_str = string.format("%02d:%02d:%02d", math.floor(up/3600), math.floor((up%3600)/60), up%60)
    put(2, y, "  Uptime:", C_DIM, C_BG);     put(14, y, up_str, C_TEXT, C_BG); y = y + 1
    put(2, y, "  Address:", C_DIM, C_BG);    put(14, y, info.address:sub(1, 18), C_TEXT, C_BG); y = y + 2

    put(2, y, "Memory", C_WHITE, C_BG); y = y + 1
    local usedMem = info.memTotal - info.memFree
    local pct = info.memTotal > 0 and (usedMem / info.memTotal) or 0
    put(2, y, "  Used:", C_DIM, C_BG)
    drawBar(14, y, 20, pct)
    put(36, y, string.format("%3d%%  %s / %s", math.floor(pct*100), fmtSize(usedMem), fmtSize(info.memTotal)), C_TEXT, C_BG)
    y = y + 2

    put(2, y, "Disks", C_WHITE, C_BG); y = y + 1
    for i, d in ipairs(info.disks) do
        if y > H - 4 then break end
        local used = d.used or 0
        local total = d.size or 1
        local p = total > 0 and (used / total) or 0
        put(2, y, "  " .. diskLabel(d, i) .. ":", C_DIM, C_BG)
        drawBar(14, y, 20, p)
        put(36, y, string.format("%3d%%  %s / %s", math.floor(p*100), fmtSize(used), fmtSize(total)), C_TEXT, C_BG)
        y = y + 1
    end
    drawHelpPanel("Live info. Refreshes once per second.")
end

local function drawHardware(info, sel, scroll)
    local y = 3
    put(2, y, "Addr     Type       Details", C_WHITE, C_BG); y = y + 1
    local maxLines = H - 6
    for i = 1, maxLines do
        local idx = i + scroll
        local c = info.comps[idx]
        if c then
            local txt = string.format("%s  %-10s %s", c.addr:sub(1,8), c.type:sub(1, 10), c.detail)
            if sel == idx then
                fill(1, y, math.floor(W*0.65), 1, " ", C_SEL_FG, C_SEL_BG)
                put(2, y, txt, C_SEL_FG, C_SEL_BG)
            else
                put(2, y, txt, C_TEXT, C_BG)
            end
            y = y + 1
        end
    end
    drawHelpPanel("Connected hardware components.")
end

local function drawBoot(info, sel, defaultAddr)
    local y = 3
    put(2, y, "Boot Priority", C_WHITE, C_BG)
    put(2, y + 1, "* = default (Esc / Exit->Boot)   > = selected (Enter)", C_DIM, C_BG)
    y = y + 3
    for i, d in ipairs(info.disks) do
        local mark = (sel == i) and "> " or ((defaultAddr == d.addr) and "* " or "  ")
        drawRow(2, y, mark .. d.label .. " (" .. fmtSize(d.size) .. ")", "", sel == i)
        y = y + 1
    end
    drawHelpPanel("Enter = boot from selected disk (no fallback).")
end

local function drawExit(sel)
    local y = 3
    drawRow(1, y, "Exit to Boot", "", sel == 1); y = y + 1
    drawRow(1, y, "Shutdown",     "", sel == 2); y = y + 1
    drawHelpPanel("Press Enter to confirm.")
end

-- ==== ЗАГРУЗКА ====

local function bootFrom(address)
    if type(address) ~= "string" then return false end
    local proxy = component.proxy(address)
    if not proxy then return false end

    pcall(computer.setBootAddress, address)

    for _, path in ipairs({"/OS.lua", "/init.lua"}) do
        local ok, exists = pcall(proxy.exists, path)
        if ok and exists then
            local okH, handle = pcall(proxy.open, path, "rb")
            if okH and handle then
                local data = ""
                while true do
                    local okR, chunk = pcall(proxy.read, handle, math.huge)
                    if okR and chunk then data = data .. chunk else break end
                end
                pcall(proxy.close, handle)

                if #data == 0 then
                    -- Пустой файл — не ОС, идём дальше.
                else
                    local func, err = load(data, "=" .. path)
                    if func then
                        local success, reason = xpcall(func, debug.traceback)
                        if success then
                            clear()
                            put(1, 1, "Not a bootable OS: " .. path, C_WARN, C_BG)
                            put(1, 3, "Script returned without taking over.", C_DIM, C_BG)
                            sleepPure(0.8)
                        else
                            -- ИСПОЛЬЗУЕМ НОВУЮ ФУНКЦИЮ ДЛЯ ОШИБОК
                            clear()
                            put(1, 1, "FATAL ERROR in " .. path, C_WARN, C_BG)
                            putError(1, 3, reason, C_TEXT, C_BG)
                            put(1, H, "Press any key to try next path...", C_TEXT, C_BG)
                            repeat local e = computer.pullSignal() until e == "key_down" or e == "touch"
                        end
                    else
                        clear()
                        put(1, 1, "Syntax error in " .. path, C_WARN, C_BG)
                        putError(1, 2, err, C_TEXT, C_BG)
                        put(1, H, "Press any key to try next path...", C_TEXT, C_BG)
                        repeat local e = computer.pullSignal() until e == "key_down" or e == "touch"
                    end
                end
            end
        end
    end
    return false
end

local function tryBoot(preferred, noFallback)
    if preferred then
        bootFrom(preferred)
    end
    if not noFallback then
        for addr in component.list("filesystem") do
            if addr ~= preferred then
                bootFrom(addr)
            end
        end
    end
    return false
end

-- ==== SPLASH ====
local function splash()
    clear()
    local cpu = "Unknown"
    local ok, v = pcall(computer.getArchitecture) if ok then cpu = tostring(v) end
    local mem = 0
    ok, v = pcall(computer.totalMemory) if ok then mem = v end
    local disks = 0
    for _ in component.list("filesystem") do disks = disks + 1 end

    local lines = {
        "Phoenix - AwardBIOS v7.00",
        "",
        "Main Processor : " .. cpu,
        "Memory Testing : " .. fmtSize(mem) .. " OK",
        "",
        "Detecting drives ... " .. disks .. " found",
        "",
        "",
        "Press any key or click for Setup",
        "Auto boot in 2 seconds ..."
    }
    local sy = math.floor(H / 2 - #lines / 2)
    for i, line in ipairs(lines) do
        put(3, sy + i, line, C_TEXT, C_BG)
    end
    put(math.floor(W/2 - 12), H - 1, "Phoenix Technologies, Ltd.", C_DIM, C_BG)

    local deadline = computer.uptime() + 2.0
    while computer.uptime() < deadline do
        local name = computer.pullSignal(deadline - computer.uptime())
        if name == "key_down" or name == "touch" then return true end
    end
    return false
end

-- ==== MAIN ====
if splash() then
    local info = getSystemInfo()
    local state = {
        tab = 1, sel = 1,
        running = true,
        exitAction = nil,
        bootAddr = info.bootAddr,
        scroll = 0,
    }
    if not state.bootAddr and #info.disks > 0 then
        state.bootAddr = info.disks[1].addr
    end

    local function drawModal(x, y, w, h, title)
        fill(1, 1, W, H, " ", C_TEXT, C_BG)
        fill(x, y, w, h, " ", C_TEXT, C_MODAL_BG)
        drawFrame(x, y, w, h, C_WHITE)
        local tx = x + math.floor((w - #title) / 2)
        put(tx, y, " " .. title .. " ", C_WHITE, C_MODAL_BG)
    end

    local function showResult(title, lines)
        local w, h = math.min(W - 4, 60), math.min(#lines + 6, H - 4)
        local x, y = math.floor(W/2 - w/2), math.floor(H/2 - h/2)
        clear()
        drawModal(x, y, w, h, title)
        for i, line in ipairs(lines) do
            if y + 2 + i > y + h - 2 then break end
            put(x + 3, y + 2 + i, line, C_TEXT, C_MODAL_BG)
        end
        local btnX = x + math.floor(w/2) - 4
        fill(btnX, y + h - 2, 9, 1, " ", C_SEL_FG, C_SEL_BG)
        put(btnX, y + h - 2, " [ OK ] ", C_SEL_FG, C_SEL_BG)
        repeat local e = computer.pullSignal() until e == "key_down" or e == "touch"
    end

    local function confirmPrompt(title, msg)
        local w, h = math.min(W - 4, 60), 9
        local x, y = math.floor(W/2 - w/2), math.floor(H/2 - h/2)
        clear()
        drawModal(x, y, w, h, title)
        put(x + 3, y + 3, msg, C_WARN, C_MODAL_BG)
        put(x + 3, y + 5, "Enter = Confirm   Esc = Cancel", C_TEXT, C_MODAL_BG)
        while true do
            local e, _, _, code = computer.pullSignal()
            if e == "key_down" then
                if code == 28 then return true
                elseif code == 1 then return false end
            elseif e == "touch" then return false end
        end
    end

    local function selectDiskPrompt()
        if #info.disks == 0 then showResult("No Disks", {"No filesystems detected."}); return nil end
        local sel = 1
        local w, h = math.min(W - 4, 50), math.min(#info.disks + 6, H - 4)
        local x, y = math.floor(W/2 - w/2), math.floor(H/2 - h/2)
        while true do
            clear()
            drawModal(x, y, w, h, "Select Disk")
            for i, d in ipairs(info.disks) do
                local yy = y + 2 + i
                local label = (d.isDefaultBoot and "* " or "  ") .. d.label .. "  " .. fmtSize(d.size)
                if i == sel then
                    fill(x + 2, yy, w - 4, 1, " ", C_SEL_FG, C_SEL_BG)
                    put(x + 3, yy, label, C_SEL_FG, C_SEL_BG)
                else
                    put(x + 3, yy, label, C_TEXT, C_MODAL_BG)
                end
            end
            put(x + 3, y + h - 2, "Up/Down  Enter  Esc", C_DIM, C_MODAL_BG)
            local e, _, a1, a2 = computer.pullSignal()
            if e == "key_down" then
                if a2 == 200 then sel = sel - 1; if sel < 1 then sel = #info.disks end
                elseif a2 == 208 then sel = sel + 1; if sel > #info.disks then sel = 1 end
                elseif a2 == 28 then return info.disks[sel]
                elseif a2 == 1 then return nil end
            elseif e == "touch" then
                local idx = math.floor(a2) - (y + 3) + 1
                if idx >= 1 and idx <= #info.disks then return info.disks[idx] end
            end
        end
    end

    local function drawBenchUI(title, phase, infoText, percent, samples, maxSample)
        clear()
        fill(1, 1, W, 1, " ", C_ACTIVE_FG, C_ACTIVE_BG)
        put(math.floor(W/2 - #title/2), 1, title, C_ACTIVE_FG, C_ACTIVE_BG)
        put(2, 3, phase, C_WHITE, C_BG)
        put(2, 4, infoText or "", C_TEXT, C_BG)
        drawBar(3, 6, W - 6, percent)
        put(3, 7, string.format("Progress: %3d%%", math.floor(percent * 100)), C_TEXT, C_BG)
        if samples and #samples > 0 then
            put(3, 9, "Rate:", C_DIM, C_BG)
            local graphH, graphW = 6, math.min(W - 10, 50)
            local startSample = math.max(1, #samples - graphW + 1)
            local m = math.max(1, maxSample or 1)
            for i = 1, math.min(graphW, #samples) do
                local s = samples[startSample + i - 1] or 0
                local barH = math.floor(s / m * graphH)
                for row = 1, barH do
                    setCol(C_FILL, C_FILL)
                    gpu.set(3 + i - 1, 9 + graphH - row + 1, "█")
                end
            end
        end
        fill(1, H, W, 1, " ", C_BAR_FG, C_BAR_BG)
        put(2, H, "Esc or click to cancel", C_BAR_FG, C_BAR_BG)
    end

    local function waitCancel()
        local name, _, _, code = computer.pullSignal(0.01)
        if name == "touch" then return true end
        if name == "key_down" and code == 1 then return true end
        return false
    end

    local function benchCPU(targetDur, updateUI)
        local warmEnd = computer.uptime() + 0.2
        local warmOps = 0
        while computer.uptime() < warmEnd do
            for i = 1, 10000 do warmOps = warmOps + 1 end
        end

        local start = computer.uptime()
        local workTime, totalOps, lastUpdate = 0, 0, 0
        local samples, maxSample = {}, 1
        local cancelled = false

        while workTime < targetDur do
            local batchStart = computer.uptime()
            for i = 1, 50000 do totalOps = totalOps + 1 end
            workTime = workTime + (computer.uptime() - batchStart)

            if updateUI and (workTime - lastUpdate) > 0.2 then
                lastUpdate = workTime
                local rate = math.floor(totalOps / (workTime + 0.0001))
                table.insert(samples, rate)
                if rate > maxSample then maxSample = rate end
                local totalElapsed = computer.uptime() - start
                updateUI(workTime / targetDur, totalElapsed, rate, totalOps, samples, maxSample)
                if waitCancel() then cancelled = true break end
            end
        end

        local rate = math.floor(totalOps / (workTime + 0.0001))
        return {
            rate = rate, workTime = workTime,
            totalTime = computer.uptime() - start,
            totalOps = totalOps, cancelled = cancelled,
            samples = samples, maxSample = maxSample
        }
    end

    local function benchRAM(memSize, updateUI)
        local start = computer.uptime()
        local workTime, lastUpdate = 0, 0
        local samples, maxSample = {}, 1
        local cancelled = false
        local tbl = {}

        local totalBatches = math.ceil(memSize / 5000)
        for b = 1, totalBatches do
            local n = math.min(5000, memSize - (b - 1) * 5000)
            local batchStart = computer.uptime()
            for i = 1, n do tbl[(b - 1) * 5000 + i] = i end
            workTime = workTime + (computer.uptime() - batchStart)

            if updateUI and (workTime - lastUpdate) > 0.15 then
                lastUpdate = workTime
                local assigned = math.min(b * 5000, memSize)
                local rate = math.floor(assigned / (workTime + 0.0001))
                table.insert(samples, rate)
                if rate > maxSample then maxSample = rate end
                local totalElapsed = computer.uptime() - start
                updateUI(assigned / memSize, totalElapsed, rate, assigned, samples, maxSample)
                if waitCancel() then cancelled = true break end
            end
        end

        local assigned = math.min(totalBatches * 5000, memSize)
        local rate = math.floor(assigned / (workTime + 0.0001))

        local freeStart = computer.uptime()
        tbl = nil
        if collectgarbage then pcall(collectgarbage) end
        local freeTime = computer.uptime() - freeStart

        return {
            rate = rate, workTime = workTime,
            totalTime = computer.uptime() - start,
            freeTime = freeTime, totalAssigned = assigned,
            cancelled = cancelled, samples = samples, maxSample = maxSample
        }
    end

    local function benchDisk(proxy, iterations, updateUI)
        local testPath = "/__bios_test__.tmp"
        local testData = string.rep("X", 1024)
        local batchSize = 10
        local totalBatches = math.ceil(iterations / batchSize)
        local totalBytes = iterations * 1024

        local wOk, wh = pcall(proxy.open, testPath, "wb")
        if wOk and wh then
            for i = 1, 5 do pcall(proxy.write, wh, testData) end
            pcall(proxy.close, wh)
            pcall(proxy.remove, testPath)
        else
            return { cancelled = true, error = "Cannot open file for writing" }
        end

        local h, oerr = proxy.open(testPath, "wb")
        if not h then return { cancelled = true, error = "Cannot open: " .. tostring(oerr) } end

        local wStart = computer.uptime()
        local wWork, wLastUpdate = 0, 0
        local wSamples, wMax = {}, 1
        local wCancelled = false

        for b = 1, totalBatches do
            local n = math.min(batchSize, iterations - (b - 1) * batchSize)
            local batchStart = computer.uptime()
            for i = 1, n do pcall(proxy.write, h, testData) end
            wWork = wWork + (computer.uptime() - batchStart)

            if updateUI and (wWork - wLastUpdate) > 0.15 then
                wLastUpdate = wWork
                local done = math.min(b * batchSize, iterations) * 1024
                local rate = math.floor(done / 1024 / (wWork + 0.0001))
                table.insert(wSamples, rate)
                if rate > wMax then wMax = rate end
                local totalElapsed = computer.uptime() - wStart
                updateUI("Write phase", wWork, totalElapsed, rate, done, wSamples, wMax)
                if waitCancel() then wCancelled = true break end
            end
        end
        pcall(proxy.close, h)
        local writeWork, writeSpeed = wWork, math.floor(totalBytes / 1024 / (wWork + 0.0001))
        local writeTotal = computer.uptime() - wStart
        if wCancelled then pcall(proxy.remove, testPath); return { cancelled = true } end

        local h2, rerr = proxy.open(testPath, "rb")
        if not h2 then pcall(proxy.remove, testPath); return { cancelled = true, error = "Cannot open: " .. tostring(rerr) } end

        local rStart = computer.uptime()
        local rWork, rLastUpdate = 0, 0
        local rSamples, rMax = {}, 1
        local rCancelled = false
        local bytesRead = 0

        while true do
            local batchStart = computer.uptime()
            local chunk
            for i = 1, batchSize do
                local ok, c = pcall(proxy.read, h2, 1024)
                if not ok or not c then chunk = nil break end
                bytesRead = bytesRead + #c
                chunk = true
            end
            rWork = rWork + (computer.uptime() - batchStart)
            if not chunk then break end

            if updateUI and (rWork - rLastUpdate) > 0.15 then
                rLastUpdate = rWork
                local rate = math.floor(bytesRead / 1024 / (rWork + 0.0001))
                table.insert(rSamples, rate)
                if rate > rMax then rMax = rate end
                local totalElapsed = computer.uptime() - rStart
                updateUI("Read phase", rWork, totalElapsed, rate, bytesRead, rSamples, rMax)
                if waitCancel() then rCancelled = true break end
            end
        end
        pcall(proxy.close, h2)
        pcall(proxy.remove, testPath)

        return {
            cancelled = rCancelled,
            writeSpeed = writeSpeed, writeWork = writeWork, writeTotal = writeTotal,
            readSpeed = math.floor(bytesRead / 1024 / (rWork + 0.0001)), readWork = rWork, readTotal = computer.uptime() - rStart,
            totalBytes = totalBytes, wSamples = wSamples, wMax = wMax, rSamples = rSamples, rMax = rMax
        }
    end

    local function utilFormat(disk)
        if not confirmPrompt("Format: " .. disk.label, "ALL FILES WILL BE DELETED PERMANENTLY!") then
            showResult("Format", {"Cancelled"}); return
        end
        local proxy = component.proxy(disk.addr)
        if not proxy then showResult("Error", {"Cannot access disk"}); return end
        local count = 0
        local function removeAll(path)
            local ok, iter = pcall(proxy.list, path)
            if not ok then return end
            for _, name in ipairs(iterToList(iter)) do
                local full = (path == "/" and "/" or path .. "/") .. name
                if name:sub(-1) == "/" then
                    removeAll(full:sub(1, -2))
                    pcall(proxy.remove, full:sub(1, -2))
                else
                    pcall(proxy.remove, full)
                end
                count = count + 1
            end
        end
        removeAll("/")
        showResult("Format Complete", {"Disk: " .. disk.label, "Removed items: " .. count})
    end

    local function utilCountFiles(disk)
        local proxy = component.proxy(disk.addr)
        if not proxy then showResult("Error", {"Cannot access disk"}); return end
        local files, dirs, cancelled, lastUpdate = 0, 0, false, 0
        local function update()
            local now = computer.uptime()
            if now - lastUpdate < 0.15 then return false end
            lastUpdate = now
            clear()
            put(2, 1, "Count Files", C_WHITE, C_BG)
            put(2, 3, "Disk: " .. disk.label, C_TEXT, C_BG)
            put(2, 5, "Files: " .. files, C_TEXT, C_BG)
            put(2, 6, "Dirs: " .. dirs, C_TEXT, C_BG)
            fill(1, H, W, 1, " ", C_BAR_FG, C_BAR_BG)
            put(2, H, "Scanning... Esc/click to cancel", C_BAR_FG, C_BAR_BG)
            return waitCancel()
        end
        local function scan(path)
            if cancelled then return end
            local ok, iter = pcall(proxy.list, path)
            if not ok then return end
            for _, name in ipairs(iterToList(iter)) do
                if cancelled then return end
                local full = (path == "/" and "/" or path .. "/") .. name
                if name:sub(-1) == "/" then dirs = dirs + 1; scan(full:sub(1, -2))
                else files = files + 1 end
                if update() then cancelled = true return end
            end
        end
        scan("/")
        if cancelled then showResult("Count Files", {"Cancelled"}); return end
        showResult("File Count", {"Disk: " .. disk.label, "Files: " .. files, "Dirs: " .. dirs, "Total: " .. (files + dirs)})
    end

    local function utilCalcSize(disk)
        local proxy = component.proxy(disk.addr)
        if not proxy then showResult("Error", {"Cannot access disk"}); return end
        local total, files, cancelled, lastUpdate = 0, 0, false, 0
        local function update()
            local now = computer.uptime()
            if now - lastUpdate < 0.15 then return false end
            lastUpdate = now
            clear()
            put(2, 1, "Calculate Size", C_WHITE, C_BG)
            put(2, 3, "Disk: " .. disk.label, C_TEXT, C_BG)
            put(2, 5, "Files: " .. files, C_TEXT, C_BG)
            put(2, 6, "Size: " .. fmtSize(total), C_TEXT, C_BG)
            fill(1, H, W, 1, " ", C_BAR_FG, C_BAR_BG)
            put(2, H, "Scanning... Esc/click to cancel", C_BAR_FG, C_BAR_BG)
            return waitCancel()
        end
        local function scan(path)
            if cancelled then return end
            local ok, iter = pcall(proxy.list, path)
            if not ok then return end
            for _, name in ipairs(iterToList(iter)) do
                if cancelled then return end
                local full = (path == "/" and "/" or path .. "/") .. name
                if name:sub(-1) == "/" then scan(full:sub(1, -2))
                else
                    local okS, s = pcall(proxy.size, full)
                    if okS and s then total = total + s end
                    files = files + 1
                end
                if update() then cancelled = true return end
            end
        end
        scan("/")
        if cancelled then showResult("Calc Size", {"Cancelled"}); return end
        showResult("Size Report", {"Disk: " .. disk.label, "Files: " .. files, "Calc: " .. fmtSize(total), "Reported: " .. fmtSize(disk.used or 0), "Total: " .. fmtSize(disk.size or 0)})
    end

    local function utilSpeedTest(disk)
        local proxy = component.proxy(disk.addr)
        if not proxy then showResult("Error", {"Cannot access disk"}); return end
        local okRO, ro = pcall(proxy.isReadOnly)
        if okRO and ro then showResult("Error", {"Disk is read-only"}); return end

        local function ui(phase, p, e, r, d, s, m)
            drawBenchUI("Disk Speed Test", phase,
                string.format("%s   %d KB/s   %d KB   %.1fs",
                    phase, r, math.floor(d / 1024), e),
                p, s, m)
        end

        local r = benchDisk(proxy, 200, ui)
        if r.cancelled then
            showResult("Disk Speed Test", r.error and {r.error, "Cancelled"} or {"Cancelled"}); return
        end
        showResult("Disk Speed Results", {
            "Disk: " .. disk.label, "Data: " .. fmtSize(r.totalBytes), "",
            string.format("Write: %s   %d KB/s", fmtTime(r.writeTotal), r.writeSpeed),
            string.format("Read:  %s   %d KB/s", fmtTime(r.readTotal), r.readSpeed),
            "", "(workTime excludes UI delay)"
        })
    end

    local function utilMemoryTest()
        local total, free = 0, 0
        local ok, v = pcall(computer.totalMemory) if ok then total = v end
        ok, v = pcall(computer.freeMemory) if ok then free = v end
        local memSize = math.floor(free * 0.15 / 8)
        if memSize < 1000 then memSize = 1000 end
        if memSize > 300000 then memSize = 300000 end

        local function ui(p, e, r, d, s, m)
            drawBenchUI("Memory Test", "Allocating",
                string.format("%d / %d slots   %d /s   %.1fs", d, memSize, r, e),
                p, s, m)
        end

        local r = benchRAM(memSize, ui)
        if r.cancelled then showResult("Memory Test", {"Cancelled"}); return end
        showResult("Memory Test Results", {
            "Total RAM: " .. fmtSize(total),
            "Free before: " .. fmtSize(free),
            "Slots: " .. r.totalAssigned, "",
            string.format("Alloc rate: %d /s   (work=%.2fs, total=%.2fs)", r.rate, r.workTime, r.totalTime),
            string.format("Free time (GC): %.3fs", r.freeTime)
        })
    end

    local function utilCpuBench()
        local function ui(p, e, r, d, s, m)
            drawBenchUI("CPU Benchmark", "Running",
                string.format("%d ops   %d ops/s   %.1fs", d, r, e),
                p, s, m)
        end
        local r = benchCPU(2.0, ui)
        if r.cancelled then showResult("CPU Benchmark", {"Cancelled"}); return end
        showResult("CPU Benchmark Results", {
            "CPU: " .. info.cpu,
            string.format("Work time: %.2fs", r.workTime),
            string.format("Total time: %.2fs", r.totalTime),
            "Ops: " .. r.totalOps, "",
            string.format("Rate (clean): %d ops/s", r.rate),
            string.format("Peak: %d ops/s", r.maxSample)
        })
    end

    local function utilRefresh()
        local old = #info.comps
        info.disks = {}
        info.comps = {}
        local bootAddr = readBootAddress()
        info.bootAddr = bootAddr
        for addr, ctype in component.list() do
            local c = { addr = addr, type = ctype, detail = "" }
            local proxy = component.proxy(addr)
            if ctype == "gpu" and proxy then
                local okM, m = pcall(proxy.maxResolution)
                if okM and m then c.detail = "Max: " .. tostring(m) end
            elseif ctype == "screen" and proxy then
                local okT, t = pcall(proxy.isTouchModeInverted)
                if okT then c.detail = t and "Touch Inverted" or "Normal" end
            elseif ctype == "filesystem" and proxy then
                local okL, l = pcall(proxy.getLabel)
                local okS, s = pcall(proxy.spaceTotal)
                local okU, u = pcall(proxy.spaceUsed)
                local label = okL and l or "Unnamed"
                local size = okS and s or 0
                local used = okU and u or 0
                c.detail = label .. " (" .. fmtSize(size) .. ")"
                table.insert(info.disks, {
                    addr = addr, label = label,
                    size = size, used = used,
                    isDefaultBoot = (addr == bootAddr),
                })
            elseif ctype == "modem" and proxy then
                local okW, w = pcall(proxy.isWireless)
                c.detail = (okW and w) and "Wireless" or "Wired"
            end
            table.insert(info.comps, c)
        end
        table.sort(info.comps, function(a, b) return a.type < b.type end)
        showResult("Components Refreshed", {"Prev: " .. old, "New: " .. #info.comps, "Disks: " .. #info.disks})
    end

    local function utilOverallBench()
        local scores = { cpu = 0, mem = 0, disk = 0 }
        local details = {}

        local function uiCPU(p, e, r, d, s, m)
            drawBenchUI("Overall Benchmark", "1/3  CPU",
                string.format("%d ops   %d ops/s   %.1fs", d, r, e), p * 0.4, s, m)
        end
        local cpuR = benchCPU(1.5, uiCPU)
        if cpuR.cancelled then showResult("Benchmark", {"Cancelled"}); return end
        scores.cpu = cpuR.rate
        details[#details+1] = string.format("CPU: %d ops/s  -> %s pts", cpuR.rate, fmtScore(scores.cpu))

        local ok, v = pcall(computer.freeMemory)
        local memSize = math.floor((ok and v or 100000) * 0.1 / 8)
        if memSize < 1000 then memSize = 1000 end
        if memSize > 200000 then memSize = 200000 end

        local function uiRAM(p, e, r, d, s, m)
            drawBenchUI("Overall Benchmark", "2/3  Memory",
                string.format("%d / %d slots   %d /s", d, memSize, r), 0.4 + p * 0.25, s, m)
        end
        local ramR = benchRAM(memSize, uiRAM)
        if ramR.cancelled then showResult("Benchmark", {"Cancelled"}); return end
        scores.mem = ramR.rate
        details[#details+1] = string.format("RAM: %d /s  -> %s pts", ramR.rate, fmtScore(scores.mem))

        local testDisk = nil
        for _, d in ipairs(info.disks) do
            local proxy = component.proxy(d.addr)
            if proxy then
                local okRO, ro = pcall(proxy.isReadOnly)
                if not (okRO and ro) then testDisk = d break end
            end
        end

        local diskLine = "Disk: (none)  -> 0 pts"
        if testDisk then
            local proxy = component.proxy(testDisk.addr)
            if proxy then
                local function uiDisk(phase, w, e, r, d, s, m)
                    drawBenchUI("Overall Benchmark", "3/3  Disk " .. phase,
                        string.format("%s KB/s   %d KB", r, math.floor(d/1024)), 0.65 + (w / 1.0) * 0.35, s, m)
                end
                local dr = benchDisk(proxy, 200, uiDisk)
                if dr.cancelled then showResult("Benchmark", {"Cancelled"}); return end
                local avg = (dr.writeSpeed + dr.readSpeed) / 2
                scores.disk = avg * 10
                diskLine = string.format("%s: W:%d R:%d  -> %s pts", testDisk.label, dr.writeSpeed, dr.readSpeed, fmtScore(scores.disk))
            end
        end
        details[#details+1] = diskLine

        local base = 1000
        local total = base + scores.cpu + scores.mem + scores.disk
        local grade = "E"
        if total >= 30000000 then grade = "S"
        elseif total >= 15000000 then grade = "A"
        elseif total >= 7000000 then grade = "B"
        elseif total >= 3000000 then grade = "C"
        elseif total >= 1000000 then grade = "D" end

        local lines = {
            string.format("Grade: %s   Score: %s", grade, fmtScore(total)),
            ""
        }
        for _, l in ipairs(details) do table.insert(lines, l) end
        table.insert(lines, "")
        table.insert(lines, string.format("Base: %d", base))
        showResult("Overall Benchmark", lines)
    end

    local UTILS = {
        { name = "Format Disk",        fn = utilFormat,       needsDisk = true,  help = "Erases ALL files from selected disk." },
        { name = "Count Files",        fn = utilCountFiles,   needsDisk = true,  help = "Recursively counts files and folders." },
        { name = "Calculate Size",     fn = utilCalcSize,     needsDisk = true,  help = "Sums total size of files." },
        { name = "Disk Speed Test",    fn = utilSpeedTest,    needsDisk = true,  help = "Write/read 200 KB, measure KB/s." },
        { name = "Memory Test",        fn = utilMemoryTest,   needsDisk = false, help = "Allocate ~15% free RAM and free." },
        { name = "CPU Benchmark",      fn = utilCpuBench,     needsDisk = false, help = "2s CPU loop, clean workTime." },
        { name = "Overall Benchmark",  fn = utilOverallBench, needsDisk = false, help = "CPU + RAM + Disk. Gives grade." },
        { name = "Refresh Components", fn = utilRefresh,      needsDisk = false, help = "Rescan all components." },
    }

    local function drawUtilities(sel)
        local y = 3
        put(2, y, "Utilities", C_WHITE, C_BG); y = y + 2
        local help = ""
        for i, u in ipairs(UTILS) do
            if i == sel then
                fill(1, y, math.floor(W*0.65), 1, " ", C_SEL_FG, C_SEL_BG)
                put(2, y, " " .. u.name, C_SEL_FG, C_SEL_BG)
                help = u.help or ""
            else
                put(2, y, " " .. u.name, C_TEXT, C_BG)
            end
            y = y + 1
        end
        drawHelpPanel(help)
    end

    local function maxSel()
        if state.tab == 1 then return 5 + #info.disks end
        if state.tab == 2 then return 2 end
        if state.tab == 3 then return 0 end
        if state.tab == 4 then return math.max(1, #info.comps) end
        if state.tab == 5 then return math.max(1, #info.disks) end
        if state.tab == 6 then return #UTILS end
        if state.tab == 7 then return 2 end
        return 1
    end

    while true do
        state.running = true
        state.exitAction = nil

        local lastRefresh = 0
        while state.running do
            local now = computer.uptime()
            if now - lastRefresh >= 1.0 then
                lastRefresh = now
                refreshDynamicInfo(info)
            end

            clear()
            drawTopBar(state.tab)

            if state.tab == 1 then drawMain(info, state.sel)
            elseif state.tab == 2 then drawAdvanced(info, state.sel)
            elseif state.tab == 3 then drawSystemInfo(info)
            elseif state.tab == 4 then
                local maxLines = H - 6
                if state.sel > state.scroll + maxLines then state.scroll = state.sel - maxLines end
                if state.sel <= state.scroll then state.scroll = state.sel - 1 end
                drawHardware(info, state.sel, state.scroll)
            elseif state.tab == 5 then drawBoot(info, state.sel, state.bootAddr)
            elseif state.tab == 6 then drawUtilities(state.sel)
            elseif state.tab == 7 then drawExit(state.sel)
            end
            drawBottomBar()

            local name, _, _, code = computer.pullSignal(0.2)
            if name == "key_down" then
                if code == 203 then
                    state.tab = state.tab - 1; if state.tab < 1 then state.tab = 7 end; state.sel = 1; state.scroll = 0
                elseif code == 205 then
                    state.tab = state.tab + 1; if state.tab > 7 then state.tab = 1 end; state.sel = 1; state.scroll = 0
                elseif code == 200 then
                    if state.tab ~= 3 then
                        state.sel = state.sel - 1; if state.sel < 1 then state.sel = maxSel() end
                    end
                elseif code == 208 then
                    if state.tab ~= 3 then
                        state.sel = state.sel + 1; if state.sel > maxSel() then state.sel = 1 end
                    end
                elseif code == 28 then
                    if state.tab == 2 then
                        if state.sel == 1 then
                            state.exitAction = "reboot"; state.running = false
                        elseif state.sel == 2 then
                            state.exitAction = "shutdown"; state.running = false
                        end
                    elseif state.tab == 5 then
                        if info.disks[state.sel] then
                            state.bootAddr = info.disks[state.sel].addr
                            state.exitAction = "bootExplicit"
                            state.running = false
                        end
                    elseif state.tab == 6 then
                        local u = UTILS[state.sel]
                        if u then
                            if u.needsDisk then
                                local d = selectDiskPrompt()
                                if d then u.fn(d) end
                            else u.fn() end
                        end
                    elseif state.tab == 7 then
                        if state.sel == 1 then
                            state.exitAction = "boot"; state.running = false
                        elseif state.sel == 2 then
                            state.exitAction = "shutdown"; state.running = false
                        end
                    end
                elseif code == 1 then
                    state.exitAction = "boot"; state.running = false
                end
            end
        end

        if state.exitAction == "shutdown" then
            clear()
            put(1, 1, "Shutting down...", C_TEXT, C_BG)
            sleepPure(0.3)
            computer.shutdown()
            return
        elseif state.exitAction == "reboot" then
            clear()
            put(1, 1, "Rebooting...", C_TEXT, C_BG)
            sleepPure(0.3)
            computer.shutdown(true)
            return
        elseif state.exitAction == "boot" or state.exitAction == "bootExplicit" then
            if not state.bootAddr then
                clear()
                put(2, math.floor(H/2),     "No boot device selected.", C_WARN, C_BG)
                put(2, math.floor(H/2) + 1, "Go to Boot tab and pick a disk.", C_TEXT, C_BG)
                put(2, math.floor(H/2) + 3, "Press any key to return to setup...", C_TEXT, C_BG)
                repeat local e = computer.pullSignal() until e == "key_down" or e == "touch"
            else
                clear()
                put(1, 1, "Booting from " .. state.bootAddr:sub(1, 8) .. "...", C_TEXT, C_BG)
                sleepPure(0.3)

                local explicit = (state.exitAction == "bootExplicit")
                tryBoot(state.bootAddr, explicit)

                clear()
                put(2, math.floor(H/2), "No bootable OS found.", C_WARN, C_BG)
                if explicit then
                    put(2, math.floor(H/2) + 1, "Selected device has no /OS.lua or /init.lua.", C_TEXT, C_BG)
                else
                    put(2, math.floor(H/2) + 1, "No disk contains a bootable OS.", C_TEXT, C_BG)
                end
                put(2, math.floor(H/2) + 3, "Press any key to return to BIOS setup...", C_TEXT, C_BG)
                repeat local e = computer.pullSignal() until e == "key_down" or e == "touch"
            end
        else
            break
        end
    end

    computer.shutdown(true)
else
    clear()
    put(1, 1, "Booting...", C_TEXT, C_BG)
    sleepPure(0.3)
    tryBoot(readBootAddress(), false)
    clear()
    put(2, math.floor(H/2),     "No bootable OS found.", C_WARN, C_BG)
    put(2, math.floor(H/2) + 1, "Rebooting in 2 seconds (press any key for Setup)...", C_TEXT, C_BG)
    local t0 = computer.uptime()
    while computer.uptime() - t0 < 2.0 do
        local e = computer.pullSignal(0.1)
        if e == "key_down" or e == "touch" then break end
    end
    computer.shutdown(true)
end