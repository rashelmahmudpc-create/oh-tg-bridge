"""
openhands_client.py
-------------------
OpenHands Cloud (app.all-hands.dev) V1 REST API-এর একটি হালকা ক্লায়েন্ট।

কোনো third-party library লাগবে না — শুধু Python-এর স্ট্যান্ডার্ড লাইব্রেরি (urllib, json, ssl)।

যে এন্ডপয়েন্টগুলো ব্যবহার করা হয়েছে (সবগুলোই অফিসিয়াল OpenHands Cloud API):

  POST /api/v1/app-conversations                       -> নতুন কনভারসেশন শুরু (start task রিটার্ন করে)
  GET  /api/v1/app-conversations/start-tasks?ids=...   -> start task এর স্ট্যাটাস (READY হলে conversation id)
  POST /api/v1/app-conversations/{cid}/send-message    -> চলমান কনভারসেশনে নতুন মেসেজ (run=true দিলে এজেন্ট চলবে)
  GET  /api/v1/app-conversations?ids=...               -> কনভারসেশনের sandbox/execution স্ট্যাটাস
  GET  /api/v1/app-conversations/search?limit=...      -> আপনার সব কনভারসেশনের লিস্ট
  GET  /api/v1/conversation/{cid}/events/search        -> ইভেন্ট স্ট্রিম (পোলিং করে লাইভ আউটপুট)
  POST /api/v1/conversations/{cid}/pending-messages    -> স্যান্ডবক্স রেডি হওয়ার আগে মেসেজ কিউ করা
  GET  /api/v1/app-conversations/{cid}/file            -> স্যান্ডবক্সের ভেতরের ফাইল পড়া
  POST /api/v1/sandboxes/{sid}/pause | /resume         -> স্যান্ডবক্স থামানো / চালু করা
  GET  /api/v1/git/repositories/search                 -> কানেক্ট করা রিপো লিস্ট

Auth: দুটো হেডার-ই পাঠানো হয় (কোনটা কাজ করে তার উপর নির্ভর না করে):
      Authorization: Bearer <key>   এবং   X-Access-Token: <key>
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://app.all-hands.dev"

# start task এর সম্ভাব্য স্ট্যাটাস
START_TASK_TERMINAL_OK = "READY"
START_TASK_TERMINAL_FAIL = "ERROR"

# execution_status: এইগুলোতে পৌঁছালে এজেন্ট আর চলছে না
EXECUTION_TERMINAL = {"finished", "error", "stuck", "idle", "paused"}
EXECUTION_DONE = {"finished", "error", "stuck"}


class OpenHandsError(RuntimeError):
    """OpenHands API থেকে কোনো ত্রুটি এলে এই exception উঠবে।"""

    def __init__(self, message: str, status: int | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class OpenHandsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        retries: int = 3,
    ) -> None:
        if not api_key:
            raise OpenHandsError("OPENHANDS_API_KEY সেট করা নেই।")
        self.api_key = api_key.strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self._ssl = ssl.create_default_context()

    # ------------------------------------------------------------------ #
    #  মূল HTTP helper
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-Access-Token": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "openhands-telegram-bridge/1.0",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body=None,
        timeout: float | None = None,
    ):
        url = self.base_url + path
        if params:
            # doseq=True দরকার — ids=uuid1&ids=uuid2 এর মতো রিপিটেড প্যারামিটারের জন্য
            url = url + "?" + urllib.parse.urlencode(params, doseq=True)

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
            try:
                with urllib.request.urlopen(
                    req, timeout=timeout or self.timeout, context=self._ssl
                ) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    if not raw.strip():
                        return None
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return raw  # কিছু এন্ডপয়েন্ট (যেমন /file) প্লেইন টেক্সট দেয়
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:600]
                except Exception:
                    pass
                # 429 / 5xx হলে আবার চেষ্টা করা নিরাপদ
                if e.code == 429 or e.code >= 500:
                    last_err = OpenHandsError(
                        f"HTTP {e.code} {method} {path}: {detail}", e.code, detail
                    )
                    time.sleep(min(2 ** attempt, 8) + 0.3)
                    continue
                raise OpenHandsError(
                    f"HTTP {e.code} {method} {path}: {detail}", e.code, detail
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                time.sleep(min(2 ** attempt, 8) + 0.3)

        raise OpenHandsError(f"নেটওয়ার্ক ত্রুটি ({method} {path}): {last_err}")

    # ------------------------------------------------------------------ #
    #  Conversation জীবনচক্র
    # ------------------------------------------------------------------ #
    def start_conversation(
        self,
        text: str,
        *,
        repository: str | None = None,
        branch: str | None = None,
        title: str | None = None,
        llm_model: str | None = None,
        agent_profile_id: str | None = None,
        git_provider: str | None = None,
        system_message_suffix: str | None = None,
    ) -> dict:
        """নতুন কনভারসেশন শুরু করে। রিটার্ন: AppConversationStartTask (id, status, ...)।"""
        payload: dict = {
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
                # run=True না দিলে এজেন্ট লুপ শুরু হবে না!
                "run": True,
            },
            "agent_type": "default",
        }
        if repository:
            payload["selected_repository"] = repository
        if branch:
            payload["selected_branch"] = branch
        if title:
            payload["title"] = title[:120]
        if llm_model:
            payload["llm_model"] = llm_model
        if agent_profile_id:
            payload["agent_profile_id"] = agent_profile_id
        if git_provider:
            payload["git_provider"] = git_provider
        if system_message_suffix:
            payload["system_message_suffix"] = system_message_suffix
        return self._request("POST", "/api/v1/app-conversations", body=payload) or {}

    def get_start_task(self, task_id: str) -> dict | None:
        out = self._request(
            "GET", "/api/v1/app-conversations/start-tasks", params={"ids": [task_id]}
        )
        if isinstance(out, list) and out:
            return out[0]
        return None

    def wait_until_ready(
        self,
        task: dict,
        *,
        timeout: float = 420.0,
        interval: float = 2.5,
        on_status=None,
    ) -> dict:
        """
        start task পোলিং করে READY হওয়া পর্যন্ত অপেক্ষা করে।
        রিটার্ন: শেষ task অবজেক্ট (এতে app_conversation_id থাকবে)।
        on_status(status_str) কলব্যাক দিলে প্রতিটি নতুন স্ট্যাটাসে কল হবে।
        """
        task = dict(task or {})
        task_id = task.get("id")
        if not task_id:
            raise OpenHandsError(f"start task এ id নেই: {task}")

        if task.get("app_conversation_id") and task.get("status") == START_TASK_TERMINAL_OK:
            return task

        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            task = self.get_start_task(task_id) or task
            status = (task.get("status") or "").upper()
            if status != last_status:
                last_status = status
                if on_status:
                    try:
                        on_status(status, task.get("detail"))
                    except Exception:
                        pass
            if status == START_TASK_TERMINAL_OK and task.get("app_conversation_id"):
                return task
            if status == START_TASK_TERMINAL_FAIL:
                raise OpenHandsError(
                    f"কনভারসেশন শুরু করা যায়নি: {task.get('detail') or task.get('error') or 'অজানা কারণ'}"
                )
            time.sleep(interval)

        raise OpenHandsError(f"কনভারসেশন রেডি হতে {int(timeout)} সেকেন্ডের বেশি সময় লেগেছে।")

    def send_message(
        self,
        conversation_id: str,
        text: str,
        *,
        run: bool = True,
        image_urls: list | None = None,
    ) -> dict:
        """চলমান কনভারসেশনে মেসেজ পাঠায়। run=True মানে এজেন্ট সাথে সাথে কাজ শুরু করবে।"""
        content: list = [{"type": "text", "text": text, "cache_prompt": False}]
        if image_urls:
            content.append({"type": "image", "image_urls": list(image_urls)})
        return self._request(
            "POST",
            f"/api/v1/app-conversations/{conversation_id}/send-message",
            body={"role": "user", "content": content, "run": run},
        ) or {}

    def queue_pending_message(self, conversation_or_task_id: str, text: str) -> dict:
        """
        স্যান্ডবক্স এখনো রেডি না হলে মেসেজ সার্ভার-সাইডে কিউ করে রাখে।
        রেডি হলে নিজে থেকেই ডেলিভার হবে। (সর্বোচ্চ ১০টা কিউ করা যায়)
        """
        return self._request(
            "POST",
            f"/api/v1/conversations/{conversation_or_task_id}/pending-messages",
            body={"content": [{"type": "text", "text": text}], "role": "user", "run": True},
        ) or {}

    def get_conversation(self, conversation_id: str) -> dict | None:
        out = self._request(
            "GET", "/api/v1/app-conversations", params={"ids": [conversation_id]}
        )
        if isinstance(out, list) and out:
            return out[0]
        return None

    def search_conversations(self, limit: int = 20) -> list:
        out = self._request(
            "GET", "/api/v1/app-conversations/search", params={"limit": limit}
        )
        if isinstance(out, dict):
            return out.get("items") or []
        if isinstance(out, list):
            return out
        return []

    def delete_conversation(self, conversation_id: str) -> dict:
        return self._request(
            "DELETE", f"/api/v1/app-conversations/{conversation_id}"
        ) or {}

    # ------------------------------------------------------------------ #
    #  Events (লাইভ আউটপুট)
    # ------------------------------------------------------------------ #
    def search_events(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        kind: str | None = None,
        since: str | None = None,
        page_id: str | None = None,
        descending: bool = False,
    ) -> dict:
        """
        ইভেন্ট লিস্ট আনে। রিটার্ন: {"items": [...], "next_page_id": ...}

        নোট: token-by-token StreamingDeltaEvent শুধু WebSocket-এ যায়, REST লগে সেভ হয় না।
        তাই এখানে ~১-২ সেকেন্ড পরপর পোলিং করে নতুন ইভেন্ট আনা হয় — বাস্তবে এটাই
        প্রায় লাইভ অনুভূতি দেয় (এজেন্টের প্রতিটি ধাপ সাথে সাথেই চলে আসে)।
        """
        params: dict = {"limit": min(100, max(1, limit))}
        if kind:
            params["kind__eq"] = kind
        if since:
            params["timestamp__gte"] = since
        if page_id:
            params["page_id"] = page_id
        if descending:
            params["sort_order"] = "TIMESTAMP_DESC"
        out = self._request(
            "GET", f"/api/v1/conversation/{conversation_id}/events/search", params=params
        )
        if isinstance(out, dict):
            return out
        if isinstance(out, list):
            return {"items": out, "next_page_id": None}
        return {"items": [], "next_page_id": None}

    def iter_new_events(
        self, conversation_id: str, *, since: str | None = None, limit: int = 100
    ):
        """pagination সামলে সব নতুন ইভেন্ট জেনারেট করে।"""
        page_id = None
        while True:
            page = self.search_events(
                conversation_id, limit=limit, since=since, page_id=page_id
            )
            for ev in page.get("items") or []:
                yield ev
            page_id = page.get("next_page_id")
            if not page_id:
                break

    # ------------------------------------------------------------------ #
    #  Sandbox + ফাইল + গিট
    # ------------------------------------------------------------------ #
    def pause_sandbox(self, sandbox_id: str) -> dict:
        return self._request("POST", f"/api/v1/sandboxes/{sandbox_id}/pause") or {}

    def resume_sandbox(self, sandbox_id: str) -> dict:
        return self._request("POST", f"/api/v1/sandboxes/{sandbox_id}/resume") or {}

    def read_file(self, conversation_id: str, file_path: str) -> str:
        """স্যান্ডবক্সের ভেতরের ফাইলের কনটেন্ট (টেক্সট)। না থাকলে খালি স্ট্রিং।"""
        out = self._request(
            "GET",
            f"/api/v1/app-conversations/{conversation_id}/file",
            params={"file_path": file_path},
        )
        if isinstance(out, str):
            return out
        if isinstance(out, dict):
            for k in ("content", "text", "data"):
                if isinstance(out.get(k), str):
                    return out[k]
            return json.dumps(out, ensure_ascii=False, indent=2)
        return str(out or "")

    def git_changes(self, conversation_id: str, repo_path: str, ref: str | None = None):
        params = {"path": repo_path}
        if ref:
            params["ref"] = ref
        return self._request(
            "GET", f"/api/v1/app-conversations/{conversation_id}/git/changes", params=params
        )

    def git_diff(self, conversation_id: str, file_path: str, ref: str | None = None):
        params = {"path": file_path}
        if ref:
            params["ref"] = ref
        return self._request(
            "GET", f"/api/v1/app-conversations/{conversation_id}/git/diff", params=params
        )

    def search_repositories(self, query: str = "", provider: str = "github", limit: int = 30) -> list:
        """provider প্যারামিটারটা বাধ্যতামূলক (না দিলে 422 এরর)।"""
        params: dict = {"provider": provider}
        if query:
            params["query"] = query
        out = self._request("GET", "/api/v1/git/repositories/search", params=params)
        if isinstance(out, dict):
            return out.get("items") or out.get("repositories") or []
        if isinstance(out, list):
            return out
        return []

    def me(self) -> dict:
        """বর্তমান ইউজারের সেটিংস — API key ঠিক আছে কিনা যাচাই করতে।"""
        out = self._request("GET", "/api/v1/users/me")
        return out if isinstance(out, dict) else {}

    def store_git_provider_token(self, provider: str, token: str, user_id: str,
                                 host: str = "github.com") -> dict:
        """GitHub/GitLab টোকেন OpenHands Cloud অ্যাকাউন্টে সেভ করে।"""
        return self._request(
            "POST", "/api/v1/secrets/git-providers",
            body={"provider_tokens": {provider: {"token": token, "user_id": user_id, "host": host}}},
        ) or {}


# ---------------------------------------------------------------------- #
#  ছোট ইউটিলিটি: conversation URL
# ---------------------------------------------------------------------- #
def conversation_url(conversation_id: str, base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/conversations/{conversation_id}"
