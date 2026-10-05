Subject: A Greek voice receptionist I built — live demo and code

Hi Techmellon team,

I built a Greek-speaking AI receptionist for a fictional dental practice, with a small web interface and a live dashboard for response times, booking outcomes, and estimated usage costs.

Demo: [Insert the tested permanent HTTPS dashboard link]
Code: https://github.com/alexkoukos/voiceagent

You can ask about services and prices, book a test appointment, or change and cancel it. Choose a voice, press “Έναρξη κλήσης”, and allow microphone access. Please use fictional details.

The main lesson was that a quick first sound is not the same as a useful answer. I separated those measurements, removed unnecessary waiting in the Greek speech pipeline, and moved booking validation and confirmation into backend checks. Greek pronunciation and interruptions also needed real conversation testing, beyond passing automated tests.

The project combines a Python backend, a realtime voice worker, Postgres, and a SwiftUI client. This is a working demo, with remaining production work documented in the repo. It shows the kind of end-to-end implementation and measurement work I’d like to contribute to your team.

I’d welcome your feedback on the implementation and whether there could be a fit at Techmellon.

Best,
Alex
