"""Entry point: python -m portfolio_health"""

from portfolio_health.server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
