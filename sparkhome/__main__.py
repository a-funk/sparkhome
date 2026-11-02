"""python3 -m sparkhome [--demo] [--port N] [--lan] [--no-browser] [--config PATH] [--selftest]"""
import argparse
import importlib
import logging


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m sparkhome", description="SparkHome: an Apple-TV-style home screen "
                                "for the DGX Spark (launcher UI, Chrome kiosk, Steam, agents).")
    p.add_argument("--demo", action="store_true", help="simulate Chrome, X11, Steam and stats (develop on a Mac)")
    p.add_argument("--port", type=int, help="HTTP port (default settings.port, 8780)")
    p.add_argument("--lan", action="store_true", help="listen on the LAN too; phones pair with the PIN shown in Settings")
    p.add_argument("--no-browser", action="store_true",
                   help="don't start the Chrome kiosk (adopt one already on the CDP port) / don't open a browser in --demo")
    p.add_argument("--config", help="config file (default ~/.config/sparkhome/sparkhome.json, else the repo's sparkhome.json)")
    p.add_argument("--selftest", action="store_true", help="run every module's offline self-checks")
    a = p.parse_args(argv)
    if a.port is not None and not 0 < a.port < 65536:
        p.error("--port must be 1-65535")
    if a.selftest:
        logging.disable(logging.CRITICAL)        # the checks provoke failures on purpose; asserts are the output
        for name in ("cdp", "inputs", "siri", "siri_audio", "hcitap", "shell", "settings", "server", "agent",
                     "connectors", "router", "plugins"):
            try:
                mod = importlib.import_module(f"sparkhome.{name}")
            except ModuleNotFoundError as e:
                if e.name != "sparkhome.agent":
                    raise
                print("agent: not present, skipped")
                continue
            mod._selftest()
            print(f"{name}: ok")
        print("selftest OK")
        return
    from .server import main as serve
    serve(a)


if __name__ == "__main__":
    main()
