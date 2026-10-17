"""Print reproducible synthetic tables. Never write into raw inputs."""
import random
import sys

rng = random.Random(2024)
print('gene,S01,S02,S03,S04')
for i in range(1, 5):
    print(f'G{i:03d},' + ','.join(str(rng.randint(5, 25)) for _ in range(4)))
print('sample,subject,group', file=sys.stderr)
for i in range(1, 5):
    print(f'S{i:02d},D{i:02d},' + ('reference' if i < 3 else 'comparison'), file=sys.stderr)
