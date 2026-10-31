(function (root) {
  "use strict";

  const cents = value => Math.round((value + Number.EPSILON) * 100) / 100;

  function estimateTrip({askingPrice, roundTripMiles, costPerMile, repairCost,
                         otherCost, budget = null, referenceMedian = null}) {
    const amounts = {askingPrice, roundTripMiles, costPerMile, repairCost, otherCost};
    if (Object.values(amounts).some(value => !Number.isFinite(value) || value < 0))
      return {valid: false, error: "Enter a nonnegative number in each cost field."};
    if (roundTripMiles > 10000 || Object.values(amounts).some(value => value > 1000000))
      return {valid: false, error: "Check the distance and cost amounts."};
    if (budget !== null && (!Number.isFinite(budget) || budget < 0 || budget > 1000000))
      return {valid: false, error: "Enter a valid budget or leave it blank."};

    const travelCost = cents(roundTripMiles * costPerMile);
    const total = cents(askingPrice + travelCost + repairCost + otherCost);
    return {
      valid: true,
      travelCost,
      total,
      budgetDifference: budget === null ? null : cents(budget - total),
      referenceDifference: Number.isFinite(referenceMedian) && referenceMedian >= 0
        ? cents(referenceMedian - total) : null
    };
  }

  root.scoutTripEstimate = estimateTrip;
  if (typeof module === "object" && module.exports) module.exports = estimateTrip;
})(globalThis);
