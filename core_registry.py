import llm_interface

class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self, description: str, parameters: dict, bucket: str = "global"):
        """Decorator to register a tool using the function's own name."""
        def decorator(func):
            tool_name = func.__name__
            self.tools[tool_name] = {
                "desc": description,
                "params": parameters,
                "handler": func,
                "bucket": bucket
            }
            return func
        return decorator

    def get_specs(self, allowed_buckets=None):
        return [
            {"type": "function", "function": {"name": n, "description": t["desc"], "parameters": t["params"]}}
            for n, t in self.tools.items()
            if allowed_buckets is None or t["bucket"] in allowed_buckets
        ]

    def execute(self, name, args):
        if name not in self.tools:
            return f"Error: Tool '{name}' not found."
        try:
            handler = self.tools[name]["handler"]
            result = handler(args)
            return llm_interface.redact_secrets(str(result))
        except Exception as e:
            return llm_interface.redact_secrets(f"Error executing {name}: {e}")

registry = ToolRegistry()
