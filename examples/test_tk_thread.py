import tkinter as tk
import threading
import time

def background(root):
    time.sleep(1)
    print("Generating event from background thread")
    try:
        root.event_generate("<<MyEvent>>", when="tail")
    except Exception as e:
        print("Error:", e)

root = tk.Tk()
def on_event(e):
    print("Main thread received event!")
    root.destroy()

root.bind("<<MyEvent>>", on_event)
threading.Thread(target=background, args=(root,), daemon=True).start()
root.mainloop()
