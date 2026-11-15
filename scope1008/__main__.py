import argparse
import logging
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="scope1008", description="Front end for the Hantek 1008A/B/C")
    ap.add_argument("--demo", action="store_true", help="start with demo signals instead of the scope")
    ap.add_argument("--debug", action="store_true", help="log USB traffic")
    ap.add_argument("--reset", action="store_true", help="forget saved settings")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from PySide6.QtWidgets import QApplication
    from .app import MainWindow, dark_palette

    app = QApplication(sys.argv)
    app.setApplicationName("Scope1008")
    dark_palette(app)
    if a.reset:
        from PySide6.QtCore import QSettings
        QSettings("Scope1008", "Scope1008").clear()
    w = MainWindow(demo=a.demo)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
