# Upstream provenance

The protocol and judger lifecycle in this package are ported from:

- Project: `saiblo/saiblo-local-judger`
- Commit: `a330c16e5349b300249655ae1b68094b4c1828b1`
- Source files: `src/core/protocol.py`, `src/core/judger.py`
- License: MIT
- Repository: https://github.com/saiblo/saiblo-local-judger

The original copyright and permission notice follows.

## Platform timeout compatibility

The selected local-debug revision deliberately comments out its `RoundConfig.time` timer in
commit `60a7c76f45e76ad02d16618e6b636c05684898d3`. Saiblo's hosted runtime separately launches AI
processes through `saiblo/simple-sandbox-wrapper` and `saiblo/simple-sandbox-daemon`, which expose
CPU/time-limit results to the platform. AA-Arena has no external Saiblo sandbox controller, so
its shared stdio judger enforces the protocol's round deadline itself and sends the existing
official `timeOutError` envelope to the game backend. The backend remains authoritative for the
terminal result; the judger does not invent a winner.

> MIT License
>
> Copyright (c) 2021 xcx
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software
> and associated documentation files (the "Software"), to deal in the Software without
> restriction, including without limitation the rights to use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
> Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or
> substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
> BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
> NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
> DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
