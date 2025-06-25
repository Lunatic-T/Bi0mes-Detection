import time
import json
import re
import os
import sys
import threading
import webbrowser
import aiohttp
import asyncio
import tkinter as tk
import discord
import urllib, urllib.parse
from discord import Webhook
from discord import Embed
from pathlib import Path
import hashlib

def safe_var_name(log_path):
    return hashlib.md5(str(log_path).encode()).hexdigest()

def generate_source_tag(log_path: Path) -> str:
    # Create a friendly, unique source tag from the path
    folder_part = log_path.parent.parent.name.replace(" ", "")[:6]
    hash_part = hashlib.md5(str(log_path).encode()).hexdigest()[:4]
    return f"{folder_part}_{hash_part}"
    
# --- Settings ---
POLL_INTERVAL = 0.5
STALENESS_THRESHOLD = 1000  # seconds
CHECK_EVERY = 1  # seconds
UPD_COUNT = 0
restart = False

# --- Saving Functions ---
def get_base_path():
    if getattr(sys, 'frozen', False):  # Running as .exe (PyInstaller)
        return os.path.dirname(sys.executable)
    else:  # Running as .py
        return os.path.dirname(os.path.abspath(__file__))

config_path = os.path.join(get_base_path(), 'config.json')

try:
    with open(config_path, 'r') as f:
        config = json.load(f)
except Exception as e:
    print(f"Failed to load config.json: {e}")
    input("\n\n\n\n\033[91mWhere is the config.json file? did you remove it? press enter to exit")
    sys.exit()
    
def save(key, value):
    config[key] = value  # update in memory
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"Failed to update config.json: {e}")

# --- Globals ---
def init_globals_for_tag(tag: str):
    globals()[f"CurrentBiome_{tag}"] = None
    globals()[f"CurrentEquipped_{tag}"] = None
    globals()[f"CurrentUsername_{tag}"] = None
    globals()[f"CurrentAccessCode_{tag}"] = None

webhooklink = config.get("webhookLink", "")
webhookroleid = config.get("webhookRoleID", "")
lastline = ""

# --- Log Matching Pattern ---
rpc_pattern = re.compile(r'\[BloxstrapRPC\] (.*)')
disconnection_pattern = re.compile(r'\[FLog\:\:Network\] Client\:Disconnect (.*)')

# --- Webhooks ---
async def webhooksend(embeds, content=None):
    if not isinstance(embeds, list):
        embeds = [embeds]
    async with aiohttp.ClientSession() as session:
        webhook = Webhook.from_url(webhooklink, session=session)
        await webhook.send(
            embeds=embeds,
            content=content
        )

# --- Integer Check Function ---
def safe_int(value):
    try:
        return int(value)
    except ValueError:
        return None

# --- Log Files Locator ---

def find_all_roblox_logs():
    base_web = Path.home() / "AppData" / "Local" 
    base_uwp = Path.home() / "AppData" / "Local" / "Packages"
    all_logs = []

    for folder in base_web.glob("Roblox"):
        log_dir = folder / "logs"
        if log_dir.exists():
            log_files = list(log_dir.glob("*.log"))
            if log_files:
                latest_log = max(log_files, key=lambda f: f.stat().st_mtime)
                if time.time() - latest_log.stat().st_mtime > STALENESS_THRESHOLD:
                    continue
                all_logs.append(latest_log)
                
    for folder in base_uwp.glob("ROBLOXCORPORATION*"):
        log_dir = folder / "LocalState" / "logs"
        if log_dir.exists():
            log_files = list(log_dir.glob("*.log"))
            if log_files:
                latest_log = max(log_files, key=lambda f: f.stat().st_mtime)
                if time.time() - latest_log.stat().st_mtime > STALENESS_THRESHOLD:
                    continue
                all_logs.append(latest_log)

    return all_logs
    
    
# --- return accessCode for each instance if exists ---
def parse_access_code_from_all_logs(log_path: Path):
    log_dir = log_path.parent
    log_files = sorted(log_dir.glob("*.log"), key=os.path.getmtime, reverse=True)  # Newest first

    access_code_regex = re.compile(r'"accessCode"\s*:\s*"([0-9a-fA-F\-]{36})"')

    for file in log_files:
        try:
            with open(file, "r", encoding="utf-8", errors="ignore") as f:
                for line in reversed(f.readlines()):
                    match = access_code_regex.search(line)
                    if match:
                        access_code = match.group(1)
                        print(f"\033[94m[{file.name}] \033[92maccessCode: \033[96m{access_code}")
                        return access_code
        except Exception as e:
            print(f"\033[96m[ERROR] \033[96mProblem Reading {file.name}: \033[91m{e}\033[0m ")

    print("\033[93m[WARN] \033[96mNo accessCode found in any logs.")
    return None
    
# --- return usernames for each instance if exists ---
def parse_username_from_all_logs(log_path: Path):
    log_dir = log_path.parent
    log_files = sorted(log_dir.glob("*.log"), key=os.path.getmtime, reverse=True)  # newest first

    for file in log_files:
        try:
            with open(file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            for line in reversed(lines):
                if "doTeleport: joinScriptUrl" in line:
                    url_match = re.search(r'https://assetgame\.roblox\.com/Game/Join\.ashx\?[^ ]+', line)
                    if url_match:
                        ticket_match = re.search(r'ticket=([^&]+)', url_match.group(0))
                        if ticket_match:
                            ticket_raw = ticket_match.group(1)
                            ticket_json_str = urllib.parse.unquote(ticket_raw)
                            # Try partial parsing even if it's cut off
                            username_match = re.search(r'"UserName"\s*:\s*"([^"]+)"', ticket_json_str)
                            if username_match:
                                username = username_match.group(1)
                                print(f"\033[94m[{file.name}] \033[92mUser: \033[96m{username}")
                                return username
        except Exception as e:
            print(f"\033[91m[ERROR] \033[96mProblem Reading {file.name}: \033[91m{e}\033[0m ")

    print("\033[93m[WARN] \033[96mNo username found in any logs.")
    return None
    
# --- Detection of Biomes in Log Files ---
async def tail_log_and_update(log_path):
    global UPD_COUNT
    global restart 
    global webhookroleid
    log_name = log_path.name
    source_tag = generate_source_tag(log_path)
    init_globals_for_tag(source_tag)

    print(f"\033[94m[{log_name}] \033[92mStr of Monitored Obj is \033[96m{source_tag}")

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        if globals()[f"CurrentAccessCode_{source_tag}"] is None:
            accesscodematch = parse_access_code_from_all_logs(log_path)
            if accesscodematch:
                globals()[f"CurrentAccessCode_{source_tag}"] = accesscodematch
                
        accessCodeofInstance = globals()[f"CurrentAccessCode_{source_tag}"]
        if globals()[f"CurrentUsername_{source_tag}"] is None:
            username = parse_username_from_all_logs(log_path)
            if username:
                globals()[f"CurrentUsername_{source_tag}"] = username
        userNameofInstance = globals()[f"CurrentUsername_{source_tag} "]
        f.seek(0, os.SEEK_END)
        while True:
            if restart:
                print("\033[96mending loop...\033[0m")
                await asyncio.sleep(3)
                restart = False
                break
            line = f.readline()
            if not line:
                await asyncio.sleep(0.25)
                continue

            line = line.strip()
            match = rpc_pattern.search(line)
            if "doTeleport: joinScriptUrl" in line:
                url_match = re.search(r'https://assetgame\.roblox\.com/Game/Join\.ashx\?[^ ]+', line)
                if url_match:
                    ticket_match = re.search(r'ticket=([^&]+)', url_match.group(0))
                    if ticket_match:
                        ticket_raw = ticket_match.group(1)
                        ticket_json_str = urllib.parse.unquote(ticket_raw)
                        # Try partial parsing even if it's cut off
                        username_match = re.search(r'"UserName"\s*:\s*"([^"]+)"', ticket_json_str)
                        if username_match:
                            if username_match != userNameofInstance:
                                globals()[f"CurrentUsername_{source_tag}"] = username_match.group(1)
                                username = username_match.group(1)
                                userNameofInstance = username
                                print("\033[94m[Username] New Username Defined:", accessCodeofInstance)
                            
            access_code_regex = re.compile(r'"accessCode"\s*:\s*"([0-9a-fA-F\-]{36})"')
            accessmatched = access_code_regex.search(line)
            if accessmatched:
                globals()[f"CurrentAccessCode_{source_tag}"] = accessmatched.group(1)
                accessCodeofInstance = accessmatched.group(1)
                print("\033[94m[accessCode] \033[92mNew accessCode Defined:", accessCodeofInstance)
                
            isdisconnected = disconnection_pattern.search(line)
            if isdisconnected:
                print("disconnected!?")
                
            if not match:
                continue
            try:
                result = json.loads(match.group(1))
            except json.JSONDecodeError:
                print(f"[{userNameofInstance}] Invalid JSON: {line}")
                continue
            biome = result["data"]["largeImage"].get("hoverText")
            aura = result["data"].get("state").replace('Equipped ', '').replace('"', '')

            # Access dynamic variables based on tag
            lastbiome = globals()[f"CurrentBiome_{source_tag}"]
            lastaura = globals()[f"CurrentEquipped_{source_tag}"]
            
            
            sendbiome = biome != lastbiome
            sendaura = aura != lastaura
            
            globals()[f"CurrentBiome_{source_tag}"] = biome
            globals()[f"CurrentEquipped_{source_tag}"] = aura
            
            if sendbiome:
                print(f"\033[94m[{userNameofInstance}] \033[92mBiome:\033[96m {biome} \033[0m")
            if sendaura:
                print(f"\033[94m[{userNameofInstance}] \033[92mEquipped:\033[96m {aura} \033[0m")

            UPD_COUNT += 1
            if sendbiome and biome != "NORMAL":
                ping = ""
                color = 0xFFFFFF
                if biome == "GLITCH":
                    ping = webhookroleid
                    color = 0x55ff55
                elif biome == "DREAMSPACE":
                    ping = webhookroleid
                    color = 0xffb6c1

                embed = discord.Embed(
                    description=f"> ## {biome.lower()} started\n> Checked: <t:{int(time.time())}:R>",
                    color=color
                )
                embed.set_footer(text=f"Bi0mes | Biome Detected")
                embed.set_thumbnail(url=f"https://github.com/Lunatic-T/PySniper/blob/main/Icons/{biome.replace(' ', '')}.png?raw=true")

                await webhooksend([embed], f"biome in {userNameofInstance}'s server\nhttp://www.roblox.com/games/start?placeId=15532962292&accessCode={accessCodeofInstance}\n{ping}")

            if sendaura and aura not in {"_None_", "In Main Menu"}:
                equip_embed = discord.Embed(
                    description=f"> ## equipped: {aura.lower()}\n> Checked: <t:{int(time.time())}:R>",
                    color=0xad7ae1
                )
                await webhooksend([equip_embed], "")
    print("\033[96mkilled loop!\033[0m")


#-------------------------------------------------------------------------------


buttonscolor="#333"
buttonsfgcolor="#fff"
textboxbgcolor="#333"
textboxfgcolor="#fff"
bgcolor="#444"
topbarcolor="#222"
labelfgcolor="#fff"
activebuttonbg="#777"
def start_move_advancedroot(event):
    root._drag_start_x = event.x_root
    root._drag_start_y = event.y_root

def do_move_advancedroot(event):
    dx = event.x_root - root._drag_start_x
    dy = event.y_root - root._drag_start_y
    x = root.winfo_x() + dx
    y = root.winfo_y() + dy
    root.geometry(f"+{x}+{y}")
    root._drag_start_x = event.x_root
    root._drag_start_y = event.y_root

def add_labeled_entry(parent, label_text, default="", row=0):
    y = 22 * row + 2
    entry = tk.Entry(parent, bg=textboxbgcolor, fg=textboxfgcolor, bd=0,
                     font=("Segoe UI", 10, "bold"), highlightthickness=0)
    entry.insert(0, default)
    entry.place(x=2, y=y, width=180, height=20)
    return entry
    
def add_label(parent, label_text, default="", row=0):
    y = 22 * row + 2
    label = tk.Label(parent, text=label_text, bg=textboxbgcolor, fg=textboxfgcolor, bd=0,
                     font=("Segoe UI", 10, "bold"), highlightthickness=0)
    label.insert(0, default)
    label.place(x=2, y=y, width=180, height=20)
    return label

def add_toggle(parent, label_text, variable, row=0):
    frame = tk.Frame(parent, bg=bgcolor)
    frame.pack(pady=2, anchor="w", padx=2)
    check = tk.Checkbutton(frame, text=label_text, variable=variable, bg=bgcolor, fg="white", activebackground=buttonscolor, activeforeground="white", selectcolor="#333", font=("Segoe UI", 10, "bold"))
    check.pack(side="right", anchor="w")
    return check

def add_button(parent, text, command,row=0):
    btn = tk.Button(parent, text=text, command=command, bg=buttonscolor, fg=buttonsfgcolor,
                    activeforeground="black", highlightthickness=0, bd=0, takefocus=0,
                    font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2")
    y = 22 * row + 2  # spacing: 40px tall buttons, 10px top margin
    btn.place(x=2, y=y, width=180, height=20)  # full width of 500px window minus 10px margins
    return btn
    
root = tk.Tk()
root.title("Help")
root.configure(bg=bgcolor)
root.geometry('500x110+50+50')
root.resizable(False, False)
root.attributes('-topmost', 1)
root.overrideredirect(True)

topbar = tk.Frame(root, bg="#222", height=20)
topbar.place(x=0, y=0, relwidth=1)

SettingsFrame = tk.Frame(root, bg="#444")
SettingsFrame.place(x=0, y=20, relwidth=1, relheight=1, height=-20)

TopBarLabel = tk.Label(topbar, text="Bi0mes \\ v1.1.0", bg=topbar['bg'], fg=labelfgcolor, font=("Segoe UI", 10, "bold"))
TopBarLabel.place(relx=0.5, rely=0.5, anchor="center")

x = (root.winfo_screenwidth() // 2) - (root.winfo_width() // 2)
y = (root.winfo_screenheight() // 2) - (root.winfo_height() // 2)

SettingsFrame.bind("<Button-1>", start_move_advancedroot)
SettingsFrame.bind("<Button-1>", start_move_advancedroot)

topbar.bind("<Button-1>", start_move_advancedroot)
topbar.bind("<B1-Motion>", do_move_advancedroot)

TopBarLabel.bind("<Button-1>", start_move_advancedroot)
TopBarLabel.bind("<B1-Motion>", do_move_advancedroot)

def close():
    runloop = False
    root.destroy()
    
def value(item):
    return item.get()
    
def saveentry(keyvalue, item):
    ok = item.get() 
    print("saving entry" if ok else "yeah uh thats not valid lmao")
    save(str(keyvalue), ok)
    
root.geometry(f'+{x}+{y}') 
def Webhooksave(event):
    global webhooklink
    global webhookroleid
    link_text = Webhooklnk.get()  # extract text
    weblink = re.search(r'https?://\S+', link_text)
    webmention = Webhookmentionroleid.get()
    i, o = weblink, webmention
    ii = i
    if ii:
        ii = i.group()
    else:
        ii = None
    print(i)
    if safe_int(o):
        print("set webhook roleid ping:", o)
        webhookroleid = o
        save("webhookRoleID", o)
    else:
        print("not a number. (roleid)")
        webhookroleid = ""

    if ii != None:
        print("set webhook link:", ii)
        webhooklink = ii
        save("webhookLink", ii)
    else:
        print("not a link. (webhooklink)")
        webhooklink = ""
    print(webhooklink, " ", webhookroleid)
    
    
close_btn = tk.Button(topbar, text="X", command=close, bg="#111", fg="white", bd=0, font=("Segoe UI", 8, "bold"), takefocus=0, highlightthickness=0)
close_btn.place(x=468, y=2, width=30, height=16)

def settextfromsavedinstance(uielement, name):
    value = config.get(name, "")
    uielement.delete(0, tk.END)
    uielement.insert(0, value)

row = 0
taskrunning = False
testreboot = False

async def start_all_log_watchers():
    global UPD_COUNT, restart, taskrunning
    print("okman")

    while True:
        if not taskrunning:
            taskrunning = True
            all_log_paths = find_all_roblox_logs()
            tasks = [asyncio.create_task(tail_log_and_update(path)) for path in all_log_paths]

            # Run all tasks in the background
            watcher_group = asyncio.gather(*tasks)

        last_upd = UPD_COUNT
        await asyncio.sleep(300)
            
        if not restart and UPD_COUNT == last_upd:
            print("\033[91m[SYSTEM] \033[94mRebooting...")
            restart = True

            # Wait for restart signal to clear
            while restart:
                await asyncio.sleep(0.1)

            print("\033[91m[SYSTEM] \033[94mSuccessfully rebooted.")
            taskrunning = False

def run_asyncio_loop():
    asyncio.run(start_all_log_watchers())

def run():
    threading.Thread(target=run_asyncio_loop, daemon=True).start()

async def oke(): 
    while True:
        randomserver.config(text=f"Updates: {UPD_COUNT}")
        await asyncio.sleep(1)
        
def okbro():
    global testreboot
    testreboot = True
    asyncio.run(oke())

def op():
    threading.Thread(target=okbro, daemon=True).start()
    
def printokbro():
    print("ok bro clicked the button smh")
    
Webhooklnk = add_labeled_entry(SettingsFrame, "Web Delay", row=row); row += 1
Webhooklnk.bind("<Return>", Webhooksave)
Webhookmentionroleid = add_labeled_entry(SettingsFrame, "Webhook Role mention", row=row); row += 1
Webhookmentionroleid.bind("<Return>", Webhooksave)

settextfromsavedinstance(Webhooklnk, "webhookLink")
settextfromsavedinstance(Webhookmentionroleid, "webhookRoleID")

toggleloop = add_button(SettingsFrame, "Detect Biomes", run, row=row); row += 1
randomserver = add_button(SettingsFrame, "wip", printokbro, row=row); row += 1
op()
root.mainloop()
