![OpenFly](assets/OpenFly.png)

# OpenFly

A fruit fly central nervous system, simulated from the public MaleCNS v1.0
connectome (166,700 neurons, 25.6 million connections), wired to the Indian
options market through OpenAlgo. One strategy to start: an intraday short
straddle on current-week NIFTY options with a combined stop loss, flat
before 15:15 every day.

The fly is asked a volatility question, not a direction question: will
NIFTY move less over the next hour than the straddle premium implies? Its
spike counts feed a measured readout, the readout feeds a conventional
straddle engine, and a reject-only guard, an intent-before-send ledger and
OpenAlgo's analyzer (paper) mode sit between the fly and any real order.

Status: planning. The plan is in [docs/PLAN.md](docs/PLAN.md). Nothing
trades yet.

## What it will do

- Download and verify the MaleCNS v1.0 flat connectome, compile it to a
  sparse graph, and simulate it with an event-driven leaky integrate-and-fire
  kernel written in numba. No C++ toolchain, runs on Windows.
- Turn NIFTY bars, INDIAVIX and the live straddle premium into
  photoreceptor currents through declared encoders.
- Read spike counts out of named populations (descending neurons, mushroom
  body output neurons, a fixed random sample) and fit a regularized readout
  against realized-versus-implied movement, chronologically, with controls.
- Run the straddle: ATM by synthetic forward, lot 65, product NRML, basket
  legs, combined stop loss and target on the summed premium, time exit at
  15:15, session calendar, reconciliation.
- Show all of it in a browser: what the fly saw, what it spiked, what the
  readout predicted, what the guard did, the straddle's live premium against
  its stop and target, and P&L against baselines.

## What it is not

A connectome is a wiring diagram, not a strategy. No profitable learning by
a connectome simulation has been demonstrated anywhere and none is claimed
here. The experiment harness exists to find out whether the network carries
information about NIFTY's next hour; if it does not, OpenFly says so and the
fixed-time straddle with its combined stop is what runs, in paper mode.

## Documents

- [docs/PLAN.md](docs/PLAN.md): what will be built, in what order, and why.
- [docs/nifty-market-facts.md](docs/nifty-market-facts.md): measured index, VIX, straddle, margin and cost numbers.
- [docs/openalgo-notes.md](docs/openalgo-notes.md): the OpenAlgo endpoints, formats and gotchas OpenFly relies on.

## Stack

Backend: Python 3.12 via uv, numba, numpy, pandas, pyarrow, FastAPI,
SQLite, the openalgo SDK. Frontend: Vite, React, TypeScript, Tailwind v4,
shadcn, lightweight-charts. Data: MaleCNS v1.0 (CC-BY 4.0; HHMI Janelia
FlyEM, University of Cambridge, MRC LMB, Google Research).

## Credits

- MaleCNS connectome: https://male-cns.janelia.org/
- Inspired by stonkfly: https://github.com/nftechie/stonkfly
- OpenAlgo: https://github.com/marketcalls/openalgo

## License

MIT. See [LICENSE](LICENSE).
