# Future.news execution integration: research findings

Checked 2026-09-30. Live execution into the user's Future account is **not yet verified**. Monitoring a public Polymarket trader and generating proposed orders does not establish that those orders can be submitted to Future.

## What the public evidence establishes

The public [Future trending page](https://future.news/trending) includes these application translation strings in its `__NEXT_DATA__` payload:

- “Import Polymarket Account” and “Enter your Polymarket account private key”.
- “Found Polymarket Safe account” and “No Polymarket account found for this private key.”
- “Export Private Key” and the wallet subtitle “Polymarket Account”.
- “Odds via Polymarket, aggregated by Future.”

These strings indicate that Future has interface support for Polymarket-linked wallets, including a Safe import flow. They do **not** prove the wallet type of this user's account, current availability of export/import, externally submitted order visibility, or a supported third-party execution API.

No developer/API documentation link appeared in the inspected public page. Bounded requests to `https://future.news/docs`, `/api`, `/robots.txt`, and `https://docs.future.news` were blocked with HTTP 403 (the subdomain failed at the network tunnel). This is an access limitation, **not evidence that these routes or documentation do not exist**. No Future execution endpoint, authentication contract, or supported automation permission has been verified. No private APIs were reverse engineered, credentials requested, or orders submitted.

## Conditional route through Polymarket

Polymarket publishes supported [wallet and authentication documentation](https://docs.polymarket.com/trading/wallets-auth) and an [order quickstart](https://docs.polymarket.com/trading/quickstart). Its documented clients connect a signer to an existing account wallet. The API flow uses an ownership signature to create/derive CLOB credentials, then authenticates private requests, including order placement.

The current docs distinguish legacy Proxy and Safe wallets from Deposit Wallets (the default for Polymarket account wallets deployed on or after May 4, 2026). Their examples resolve both signer and account wallet, which can be different addresses. Do not assume that a Future account always uses a Safe or that the login address is the trading wallet.

**If** Future uses the exact same user-controlled Polymarket wallet and that wallet has a supported signer/authentication method, an integration may be able to execute through Polymarket's documented client using that wallet's funds and positions. This is conditional interoperability, not a verified Future API integration. Future's display of externally submitted trades, activity history, rewards, referrals, and other application behavior still need confirmation.

Polymarket also documents [Session Keys](https://docs.polymarket.com/trading/session-keys) for scoped trading access to Deposit Wallets. This is a possible authentication option only after identifying the wallet type and confirming account support; it must not be assumed available for Future's Safe import flow.

## Concrete information needed before live implementation

1. The user's **public Future trading-wallet address** and wallet type, plus which supported signer method controls it. A public address is sufficient for initial identity checks; never paste private keys or session credentials into chat.
2. Future's official API documentation or written support guidance covering external bots, authentication, order submission/cancellation, order status/fills, and limits. The inspected page links [Future's Discord](https://discord.gg/SetGEXqMET) and [official X account](https://x.com/futuredotnews); neither has been contacted.
3. Confirmation of whether the requirement means submitting through Future's own service, or using the same underlying wallet through Polymarket with resulting positions visible in Future. Only the latter has a potentially documented execution route at present.
4. Confirmation that externally submitted orders for the exact wallet are reflected in Future, and verification of its supported wallet/signature configuration. A separate newly created Polymarket wallet would not satisfy the requested destination.

Until these are resolved, any bot artifact should identify itself as monitoring/paper execution and keep live execution unavailable. It should not invent a Future adapter or claim that a generic Polymarket order reaches the user's Future account.
