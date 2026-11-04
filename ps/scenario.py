"""Operating-state input selection and observation of unchanged NPU labels.

The requested class selects recorded input windows only. It is never passed to
the inference worker or used to replace a prediction. Call under Replay.guard.
"""


class FaultScenario:
    def __init__(self, labels, started):
        self.pools = {label: [] for label in range(22)}
        for index, label in enumerate(labels):
            self.pools[label].append(index)
        if any(not pool for pool in self.pools.values()):
            raise ValueError("Operating scenario requires recorded windows for all 22 classes")
        self.positions = {label: 0 for label in self.pools}
        self.started = started
        self.requests = []
        self.events = []
        self.previous = None
        self.event_count = 0
        self.request(0, started)

    def elapsed(self, now):
        return round((now - self.started) * 1000, 3)

    def request(self, label, now):
        if type(label) is not int or label not in self.pools:
            raise ValueError("Fault label must be an integer from 0 to 21")
        if self.requests and self.requests[-1]['label'] == label:
            return dict(self.requests[-1])
        item = dict(id=self.requests[-1]['id'] + 1 if self.requests else 1,
                    label=label, requested_ms=self.elapsed(now), first_input_ms=None,
                    first_match_ms=None, first_match_sequence=None,
                    command_to_result_ms=None, input_to_result_ms=None)
        self.requests = (self.requests + [item])[-100:]
        return dict(item)

    def select(self, now):
        request = self.requests[-1]
        label = request['label']
        position = self.positions[label]
        pool = self.pools[label]
        self.positions[label] += 1
        if request['first_input_ms'] is None:
            request['first_input_ms'] = self.elapsed(now)
        return pool[position % len(pool)], dict(
            request_id=request['id'], input_label=label,
            request_ms=request['requested_ms'], first_input_ms=request['first_input_ms'],
            selected_ms=self.elapsed(now), source_cycle=position // len(pool))

    def observe(self, prediction, sequence, now, context):
        published_ms = self.elapsed(now)
        event = None
        if prediction != self.previous:
            if self.previous is None:
                kind = 'initial'
            elif prediction == 0:
                kind = 'recovered'
            elif self.previous == 0:
                kind = 'fault_detected'
            else:
                kind = 'fault_changed'
            self.event_count += 1
            event = dict(id=self.event_count, kind=kind, from_label=self.previous,
                         label=prediction, sequence=sequence, published_ms=published_ms,
                         request_id=context['request_id'])
            self.events = (self.events + [event])[-100:]
            self.previous = prediction
        # This comparison measures the scenario response after classification;
        # neither the label nor the event above is corrected by the reference.
        request = next((r for r in self.requests if r['id'] == context['request_id']), None)
        if request and request['first_match_ms'] is None and prediction == request['label']:
            request.update(first_match_ms=published_ms, first_match_sequence=sequence,
                           command_to_result_ms=round(published_ms-request['requested_ms'], 3),
                           input_to_result_ms=round(published_ms-request['first_input_ms'], 3))
        return event

    def public(self):
        return dict(enabled=True, source='recorded-window-injection',
                    requested_label=self.requests[-1]['label'], predicted_label=self.previous,
                    requests=[dict(r) for r in self.requests], events=[dict(e) for e in self.events],
                    event_count=self.event_count)
