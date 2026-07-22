## I1
One-Sentence Story: Use audio tracks from internet human video to label contact moments automatically, creating structured pseudo-supervision for manipulation pretraining and injecting contact structure into video representations.
Theme: Data Engines
Form: new mechanism or new problem
Summary: Manipulation learning lacks contact-level labels. Impacts, placements, and collisions produce clear audio transients, so egocentric video from sources such as Ego4D and EPIC can provide contact timestamps at no annotation cost. Use those timestamps as an auxiliary objective for video-representation pretraining, then transfer the representation to manipulation policies.
Minimal Falsification Experiment: Generate contact pseudo-labels from audio transients on an Ego4D subset, pretrain a video encoder, and compare low-data manipulation BC against the same encoder without contact supervision using 1×H100 for one week. Kill the idea if downstream success does not improve significantly.
Why It May Be Novel: Automatic audio labeling of contact moments has not been shown for manipulation pretraining. This remains a hypothesis for independent verification.
