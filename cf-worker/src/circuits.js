/**
 * Circuit lat/lon for the rain radar map, keyed by circuit_short_name (the
 * same field the pit-loss table in Django's settings.py is keyed by).
 * Approximate (main grandstand/paddock area), which is plenty precise for
 * a radar overlay at the zoom level we use.
 *
 * Direct port of the root project's circuits.py -- keep both in sync if you
 * add a circuit (or delete circuits.py/replay.py/poller.py at the repo root
 * now that this worker replaces them; see the top-level README).
 */

export const CIRCUIT_COORDINATES = {
  "Monaco": [43.7347, 7.4206],
  "Singapore": [1.2914, 103.8640],
  "Spa-Francorchamps": [50.4372, 5.9714],
  "Monza": [45.6156, 9.2811],
  "Silverstone": [52.0786, -1.0169],
  "Suzuka": [34.8431, 136.5410],
  "Baku": [40.3725, 49.8533],
  "Jeddah": [21.6319, 39.1044],
  "Las Vegas": [36.1147, -115.1728],
  "Yas Marina": [24.4672, 54.6031],
  "Bahrain": [26.0325, 50.5106],
  "Zandvoort": [52.3888, 4.5409],
  "Hungaroring": [47.5789, 19.2486],
  "Interlagos": [-23.7036, -46.6997],
  "Miami": [25.9581, -80.2389],
  "Austin": [30.1328, -97.6411],
  "Mexico City": [19.4042, -99.0907],
  "Melbourne": [-37.8497, 144.9680],
  "Shanghai": [31.3389, 121.2196],
  "Imola": [44.3439, 11.7167],
  "Barcelona": [41.5700, 2.2611],
  "Montreal": [45.5000, -73.5228],
};
