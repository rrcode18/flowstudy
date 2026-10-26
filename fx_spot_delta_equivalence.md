# FX Spot Exposure: Equivalence of Basel 2.5 DCF and FRTB SA

*Technical note • 26 October 2026*

## 1. Result and scope

For a portfolio whose reporting-currency value is linear in FX spot rates, the **Basel 2.5 discounted-cashflow (DCF) currency exposure equals the FRTB SA FX delta sensitivity**, provided both calculations use the same cashflow coverage and valuation assumptions.

The equality concerns **reporting-currency FX exposure**, not capital charges or interest-rate sensitivities. It applies to vanilla cash, FX spot and forwards, fixed-notional cross-currency swaps, single-currency interest-rate swaps, OIS and FRAs. Interest-rate futures require consistent treatment of daily settlement and margin cash.

## 2. Definitions and assumptions

Let \(R\) be the reporting currency and \(S_c\) the price of one unit of currency \(c\), expressed in \(R\), with \(S_R=1\). Define the signed local-currency PV:

\[
P_c=\sum_i D_c(t,T_i)\,\widehat{\mathrm{CF}}_{c,i}. \tag{1}
\]

Receipts are positive and payments negative. Floating cashflows are projected using the applicable curves. Discount factors reflect the valuation’s collateral and basis conventions; they need not be standalone domestic OIS discount factors.

Assume:

1. Both methods include the same contractual cashflows, accruals and relevant cash balances, without double-counting.
2. Discount curves, projection curves, basis and other non-FX risk factors remain fixed during the FX bump.
3. Local-currency PVs \(P_c\) are independent of the bumped spot rate: there are no unmodelled FX resets, FX-linked coupons or other spot-dependent cashflows.
4. Both calculations use the same reporting currency and consistent valuation and settlement conventions.

“Linear” here means **linear in FX spot under these assumptions**. The product need not be linear in interest rates.

## 3. Proof

The portfolio value is:

\[
V=\sum_c S_cP_c. \tag{2}
\]

Under the legacy Basel 2.5 DCF treatment, the exposure to foreign currency \(c\), translated into the reporting currency, is:

\[
E_c^{\mathrm{B2.5}}=S_cP_c. \tag{3}
\]

FRTB SA defines FX delta sensitivity using a 1% relative increase in spot, with other relevant risk factors unchanged [2]:

\[
s_c^{\mathrm{FRTB}}
=\frac{V(S_1,\ldots,1.01S_c,\ldots)-V(S_1,\ldots,S_c,\ldots)}{0.01}.
\tag{4}
\]

Substituting (2):

\[
s_c^{\mathrm{FRTB}}
=\frac{0.01S_cP_c}{0.01}
=S_cP_c
=E_c^{\mathrm{B2.5}}. \tag{5}
\]

This is an **exact finite-bump identity**, not a first-order approximation. The ordinary spot derivative has a different normalisation:

\[
\delta_c=\frac{\partial V}{\partial S_c}=P_c,
\qquad
s_c^{\mathrm{FRTB}}=S_c\delta_c. \tag{6}
\]

Thus, compare reporting-currency exposure with reporting-currency sensitivity—not \(P_c\) directly with \(s_c\).

## 4. Application by product

The table assumes USD reporting and EUR as the foreign currency; \(S\) denotes USD per EUR.

| Product | EUR position \(P_{\rm EUR}\) | Common USD FX exposure \(S P_{\rm EUR}\) |
|---|---|---|
| EUR cash balance | Signed cash balance \(C\) | \(SC\) |
| Unsettled FX spot: receive EUR, pay USD | PV of the EUR receipt to spot settlement | \(SND_{\rm EUR}(T_{\rm spot})\); approximately \(SN\) if settlement discounting is neglected consistently |
| FX forward: receive EUR, pay USD | \(ND_{\rm EUR}(T)\) | \(SND_{\rm EUR}(T)\) |
| Fixed-notional EUR/USD cross-currency swap | Net PV of remaining EUR coupons and contractual EUR principal exchanges | \(S\) times the EUR-leg PV |
| EUR interest-rate swap | EUR PV of receive leg minus pay leg | \(S\) times net swap PV |
| EUR OIS | EUR PV of receive leg minus pay leg, with compounded overnight coupons projected consistently | \(S\) times net OIS PV |
| EUR FRA | EUR PV of the contractual net settlement amount | \(S\) times net FRA PV |
| EUR-settled interest-rate futures | Residual local-currency contract value/unsettled settlement P&L; margin cash treated separately | \(S\) times the residual value, plus FX exposure of separately included EUR cash balances |
| USD-only cash, swap, OIS, FRA or futures | No EUR position, absent FX-linked features | Zero EUR/USD FX exposure |

For a settled FX spot transaction, the trade’s remaining flows disappear and the resulting cash balances carry the exposure. For an IRS, OIS or FRA, **do not introduce an artificial principal exchange merely because notional is used to calculate interest**.

A zero-PV single-currency swap or FRA has zero FX translation exposure under these assumptions, even if its interest-rate risk is material. A zero-PV cross-currency swap can have substantial FX exposure because its two currency-leg PVs offset only after translation.

**Futures.** Daily settlement extinguishes accumulated contract P&L [3]. Immediately after settlement, the residual contract value is normally zero, but foreign-currency settlement proceeds and margin balances may still carry FX exposure. The futures quotation multiplied by notional is not the contract’s net PV and must not be used as its FX exposure.

## 5. Illustration and limits

For a one-year forward receiving EUR 10m and paying USD 11m:

\[
V=S\,10mD_{\rm EUR}(1Y)-11mD_{\rm USD}(1Y).
\]

If \(S=1.08\) and \(D_{\rm EUR}(1Y)=0.97\), both methods give:

\[
E^{\mathrm{B2.5}}=s^{\mathrm{FRTB}}
=1.08\times10m\times0.97=\mathrm{USD}\ 10.476m.
\]

The ordinary spot derivative is EUR 9.7m; the P&L from a 1% spot increase is USD 104,760.

The equivalence is conditional, not a blanket regulatory statement. Legacy rules contain specific provisions for accrued and expected future interest [1]; cashflow exclusions can break the comparison. FX-resetting cross-currency swaps require revaluation of spot-dependent cashflows. Different curve-bump conventions can also produce different results.

**Conclusion:** With identical DCF coverage and spot-independent local-currency PVs, both frameworks measure exactly the same FX exposure for each currency. Their capital weights and aggregation rules remain different.

## References

[1] BCBS, *International Convergence of Capital Measurement and Capital Standards: A Revised Framework—Comprehensive Version*, June 2006: paragraphs **718(xxxii), 718(xxxv), 718(xxxvi) and 718(xli)**. The legacy NPV rule is reproduced in **MAR20.57**, with currency aggregation in **MAR20.59** and interest treatment in **MAR20.56**. These rules support discounted currency positions; they do not explicitly state equation (5), which is the derivation in this note.

- Original framework: https://www.bis.org/publ/bcbs128.pdf
- Historical consolidated MAR20: https://www.bis.org/basel_framework/chapter/MAR/20.htm?inforce=20191215&published=20191215

[2] BCBS, *MAR21—Standardised approach: sensitivities-based method*, **MAR21.15–21.16 and MAR21.24**: reporting-currency sensitivities, other risk factors held fixed, and the 1% relative FX bump.

https://www.bis.org/committees/bcbs/basel-framework/standard/mar/21/inforce/2023-01-01/published/2026-03-23

[3] CME Clearing, *Money Calculations for CME-cleared Futures and Options*, updated 11 June 2015, page 1: daily cash settlement variation.

https://www.cmegroup.com/clearing/files/CME-Money-Calculations-Futures-and-Options.pdf

