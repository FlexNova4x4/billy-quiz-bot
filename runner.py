import os
import sys
import time
import urllib.request
import urllib.parse
import subprocess

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROUP_ID = os.environ.get("GROUP_ID")

def send_message(text):
    if not BOT_TOKEN or not GROUP_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": GROUP_ID, "text": text}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Failed to send message: {e}")

def main():
    print("Starting bot wrapper...")
    send_message("✅ Bot is online. Quizzes ready!")
    
    start_time = time.time()
    bot_process = subprocess.Popen([sys.executable, "bot.py"])
    
    warning_sent = False
    
    while True:
        # Check if bot process died on its own
        if bot_process.poll() is not None:
            sys.exit(bot_process.returncode)
            
        elapsed_minutes = (time.time() - start_time) / 60
        
        # 340 minutes -> Send Warning
        if elapsed_minutes >= 340 and not warning_sent:
            send_message("⚠️ Bot will restart in ~10 minutes. Please finish or pause your quiz!")
            warning_sent = True
            
        # 350 minutes -> Kill Bot and Exit 0
        if elapsed_minutes >= 350:
            print("Time limit reached. Terminating bot...")
            bot_process.terminate()
            try:
                bot_process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                bot_process.kill()
            sys.exit(0)
            
        time.sleep(10)

if __name__ == "__main__":
    main()
