"""Local, masked DeepSeek setup. No API requests or platform actions."""

from __future__ import annotations

import getpass
import sys
import warnings
from pathlib import Path

from boss_agent_cli.ai.config import AIConfigStore


def save_settings(data_dir: Path, api_key: str) -> None:
    if not api_key or any(character.isspace() for character in api_key):
        raise ValueError("Invalid API key")
    store = AIConfigStore(data_dir)
    store.save_api_key(api_key)
    store.save_config(
        ai_provider="deepseek",
        ai_model="deepseek-flash",
        ai_base_url="https://api.deepseek.com",
    )


def main(data_dir: Path | None = None) -> int:
    print("DeepSeek 官方 API 配置")
    print("只在本机加密保存密钥，不调用 API、不发送简历、不投递岗位。")
    print("输入时不会显示字符；粘贴密钥后按回车。直接回车可以取消。")
    if not sys.stdin.isatty():
        print("请双击“配置DeepSeek.bat”运行，不要通过重定向输入密钥。")
        return 1
    target = data_dir or Path(__file__).resolve().parents[1] / "data" / "boss-agent"
    try:
        if (target / "ai" / "api_key.enc").exists():
            while True:
                answer = input("已有 API 密钥。是否替换？输入 yes 确认（不区分大小写），输入 no 或直接回车取消：")
                answer = answer.strip().casefold()
                if answer == "yes":
                    break
                if answer in ("", "no"):
                    print("已取消，原配置未修改。")
                    return 0
                print("没有识别这个回答。请只输入 yes 或 no，此处不要粘贴密钥。")
        # Never allow getpass to fall back to an echoed password prompt.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            api_key = getpass.getpass("请粘贴 DeepSeek API 密钥：").strip()
        if not api_key:
            print("已取消，没有保存密钥。")
            return 0
        save_settings(target, api_key)
        print("配置已保存，使用模型：deepseek-flash。")
        print("尚未联网验证密钥或余额。关闭窗口后，可打开工作台继续配置。")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n已取消配置。")
        return 130
    except getpass.GetPassWarning:
        print("当前终端无法隐藏输入，已取消。请双击配置入口重试。")
        return 1
    except Exception as exc:
        print(f"未能完成配置（{type(exc).__name__}）。请不要发送密钥，只反馈这个错误名称。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
