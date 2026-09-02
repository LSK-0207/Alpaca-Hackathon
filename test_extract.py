import json

class TextContent:
    def __init__(self, text):
        self.type = 'text'
        self.text = text

raw = [TextContent(text='{"_alpaca_mcp_security":{"trust":"untrusted"},"data":{"bars":{"NFLX":[{"c":81.05},{"c":80.81}]}}}')]
symbol = 'NFLX'

def extract_closes(data):
    closes = []
    if isinstance(data, list):
        for item in data:
            text = getattr(item, 'text', None)
            if text:
                try:
                    parsed = json.loads(text)
                    extracted = extract_closes(parsed)
                    if extracted:
                        return extracted
                except Exception as e:
                    print('ERR:', e)
    elif isinstance(data, dict):
        if 'data' in data and isinstance(data['data'], dict):
            data = data['data']
        bars = data.get('bars') or data.get(symbol) or data.get(symbol.lower()) or []
        if isinstance(bars, dict):
            bars = bars.get(symbol) or bars.get(symbol.lower()) or []
        if isinstance(bars, list):
            for bar in bars:
                if isinstance(bar, dict):
                    c = bar.get('c') or bar.get('close') or bar.get('Close')
                    if c is not None:
                        closes.append(float(c))
    return closes

print("Result:", extract_closes(raw))
