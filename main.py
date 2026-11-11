import app
from config import CONFIG

__version__ = "0.1.0"


def main():
    print(f"streak-catcher {__version__}")
    print("config:")
    print(CONFIG.describe())
    app.run()


if __name__ == "__main__":
    main()
