const assert = require("node:assert/strict");
const estimateTrip = require("../marketplace_site/trip_math.js");

const freePickup = estimateTrip({askingPrice: 0, roundTripMiles: 30,
  costPerMile: 0.5, repairCost: 20, otherCost: 5, budget: 50,
  referenceMedian: 60});
assert.deepEqual(freePickup, {valid: true, travelCost: 15, total: 40,
  budgetDifference: 10, referenceDifference: 20});

const noComparison = estimateTrip({askingPrice: 25, roundTripMiles: 18,
  costPerMile: 0.5, repairCost: 0, otherCost: 0});
assert.equal(noComparison.total, 34);
assert.equal(noComparison.referenceDifference, null);
assert.equal(noComparison.budgetDifference, null);

const overBudget = estimateTrip({askingPrice: 100, roundTripMiles: 10,
  costPerMile: 0.5, repairCost: 30, otherCost: 0, budget: 120,
  referenceMedian: 125});
assert.equal(overBudget.budgetDifference, -15);
assert.equal(overBudget.referenceDifference, -10);

assert.equal(estimateTrip({askingPrice: 0, roundTripMiles: -1,
  costPerMile: 0.5, repairCost: 0, otherCost: 0}).valid, false);
assert.equal(estimateTrip({askingPrice: Infinity, roundTripMiles: 10,
  costPerMile: 0.5, repairCost: 0, otherCost: 0}).valid, false);
assert.equal(estimateTrip({askingPrice: 0, roundTripMiles: 10,
  costPerMile: 0.5, repairCost: 0, otherCost: 0, budget: -1}).valid, false);

console.log("Trip estimates passed");
