import face_recognition
import numpy as np
import cv2
import glob
import random
import string
import os
import math
import argparse
import multiprocessing
import queue

def parseArgs():
    parser = argparse.ArgumentParser();
    parser.add_argument('-i', type=str, help='Image of target face to scan for.', required=True)
    parser.add_argument('-v', type=str, help='Video to process', required=True)
    parser.add_argument('-t', type=float, help='Tolerance of face detection, lower is stricter. (0.1-1.0)', default=0.6)
    parser.add_argument('-f', type=int, help='Amount of frames per second to extract.', default=25)
    parser.add_argument('-n', type=int, help='Number of frames with target face to save from each vid.', default=1000)
    parser.add_argument('-s', type=int, help='Minimum KB size of images to keep in the faceset.', default=32)
    parser.add_argument('-j', type=int, help='Number of worker threads to spawn', default=1)
    parser.add_argument('--skipFrames', type=int, help='Start frame', default = 0 )
    return vars(parser.parse_args())

def main( args ):
    if args['t'] > 1.0:
        args['t'] = 1.0
    elif args['t'] < 0.1:
        args['t'] = 0.1

    min_KB = args['s']
    tol = args['t']
    xfps = args['f']
    targfname = args['i']
    vid_dir = args['v']
    faces_from_each_video = args['n']
    skipFrames = args['skipFrames']

    numWorkers = args['j']
    poolWorkQueue = multiprocessing.Queue(maxsize=2*numWorkers)
    poolResultQueue = multiprocessing.Queue()
    doneEvent = multiprocessing.Event()
    pool = []
    for idx in range(numWorkers):
        proc = multiprocessing.Process(target=worker_process_func, args=(idx, poolWorkQueue, poolResultQueue, doneEvent))
        proc.start()
        pool.append(proc)

    if faces_from_each_video < 1:
        faces_from_each_video = 1000

    if min_KB < 1:
        min_KB = 32

    print("Target filename: " + targfname + ".")
    print("Video input directory: " + vid_dir + ".")
    print("Tolerance: " + str(tol) + ".")
    print("Number of confirmed faces saved from each video: " + str(faces_from_each_video) + ".")

    if(cv2.ocl.haveOpenCL()):
        cv2.ocl.setUseOpenCL(True)
        print("Using OpenCL: " + str(cv2.ocl.useOpenCL()) + ".")

    target_image = face_recognition.load_image_file(targfname)
    outdir = str(str(os.path.splitext(targfname)[0]) + "_output");
    scanned_vids = str(str(os.path.splitext(targfname)[0]) + "_scanned_vids");
    too_small = str(str(os.path.splitext(targfname)[0]) + "_too_small");

    #check if output directories already exists, and if not, create it
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(scanned_vids, exist_ok=True)
    os.makedirs(too_small, exist_ok=True)

    print("Output directory: " + outdir + ".")
    print("Scanned videos will be moved to: " + scanned_vids + ".")

    try:
        target_encoding = face_recognition.face_encodings(target_image)[0]
    except IndexError:
        print("No face found in target image.")
        raise SystemExit(0)

    def random_string(length):
        return ''.join(random.choice(string.ascii_letters) for m in range(length))

    def writeFromQueue( resQueue, imageIdx, videoIdx ):
        try:
            croppedframe = resQueue.get(block=False)
            print('Writing image ' + str(imageIdx) + '.')
            cv2.imwrite(("vid_" + str(videoIdx) + '_pic' + str(imageIdx) + '_' + random_string(15) + ".jpg"), croppedframe, [int(cv2.IMWRITE_JPEG_QUALITY), 98])
            return True
        except queue.Empty:
            return False

    for zz in range(200):
        try:
            vid = random.choice(glob.glob(vid_dir + '/*.mp4'))
            print("Now looking at video: " + vid)
            input_video = cv2.VideoCapture(vid)
            if skipFrames > 0:
                input_video.set(cv2.CAP_PROP_POS_FRAMES, skipFrames);

            framenum = skipFrames
            vidheight = input_video.get(4)
            vidwidth = input_video.get(3)
            vidfps = input_video.get(cv2.CAP_PROP_FPS)
            totalframes = input_video.get(cv2.CAP_PROP_FRAME_COUNT)
            outputsize = 256, 256

            if xfps > vidfps:
                xfps = vidfps

            print("Frame Width: " + str(vidwidth) + ", Height: " + str(vidheight) + ".")

            known_faces = [
                target_encoding
            ]

            #switch to output directory
            os.chdir(str(os.path.splitext(targfname)[0]) + "_output")

            written = 1
            while(input_video.isOpened()):
                input_video.set(1, (framenum + (vidfps/xfps)))
                framenum += vidfps/xfps
                ret, frame = input_video.read()

                if not ret:
                    break

                percentage = (framenum/totalframes)*100
                print("Queuing frame " + str(int(framenum)) + "/" + str(int(totalframes)) + str(" (%.2f%%)" % percentage))

                rgb_frame = frame[:, :, ::-1]

                if writeFromQueue( poolResultQueue, written, zz ):
                    written += 1

                poolWorkQueue.put( ( rgb_frame, tol, known_faces ) )

                #------

                if percentage > 99.9 or written > faces_from_each_video:
                    os.rename(vid, scanned_vids + '/vid' + str(zz) + '_' + random_string(5) + '.mp4')
                    try:
                        os.rename(vid, scanned_vids + '/vid' + str(zz) + '_' + random_string(5) + '.mp4')
                    except:
                        print("Failed to rename video")
                    break
            input_video.release()

        except ValueError:
            print ("Scanning videos complete.")
            doneEvent.set()
            print( "Finishing work queue. Waiting for processes to end" )
            for proc in pool:
                proc.join()
            while writeFromQueue( poolResultQueue, written, zz ):
                written += 1

            pass
        except IndexError:
            pass
    #Removes images under 32KB
    counter = 0
    low_quat = min_KB * 1000
    for xx in (os.listdir(os.getcwd())):
        if(os.path.getsize(xx)) < low_quat:
            os.rename(xx, too_small + "/too small-" + str(counter) + random_string(15) + ".jpg")
            print ("Moving " + str(xx) + " to the too small folder")
            counter += 1

    #Remove images with more than one face
    print ("Now double checking there is only one face in each photo")
    for yy in (os.listdir(os.getcwd())):
        # Load the jpg file into a numpy array
        image = face_recognition.load_image_file(yy)

        # Find all the faces in the image using a pre-trained convolutional neural network.
        # This method is more accurate than the default HOG model, but it's slower
        # unless you have an nvidia GPU and dlib compiled with CUDA extensions. But if you do,
        # this will use GPU acceleration and perform well.
        # See also: find_faces_in_picture.py
        face_locations = face_recognition.face_locations(image, number_of_times_to_upsample=0, model="cnn")

        print("I found {} face(s) in this photograph.".format(len(face_locations)))

        if not (len(face_locations)) == 1:
            os.remove(yy)
            print (str(yy) + ' was removed')

#Create worker threads
def worker_process_func(procId, workQueue, resultQueue, doneEvent):
    print("Worker {} started".format(procId))
    global tol

    while not ( doneEvent.is_set() and workQueue.empty() ):
        try:
            work = workQueue.get(block=True, timeout=1)
            rgb_frame = work[0]
            tolerance = work[1]
            known_faces = work[2]

            vidwidth = rgb_frame.shape[1]
            vidheight = rgb_frame.shape[0]

            face_locations = face_recognition.face_locations(rgb_frame)
            face_encodings = face_recognition.face_encodings(rgb_frame, face_locations)

            for fenc, floc in zip(face_encodings, face_locations):
                istarget = face_recognition.compare_faces(known_faces, fenc, tolerance=float(tolerance))

                #if the face found matches the target
                if istarget[0]:
                    top, right, bottom, left = floc
                    facefound = True
                    #squaring it up
                    if (bottom - top) > (right - left):
                        right = left + (bottom - top)
                    elif (right - left) > (bottom - top):
                        bottom = top + (right - left)
                    #calculating the diagonal of the cropped face for rotation purposes
                    #diagonal = math.sqrt(2*(bottom - top))
                    #padding = diagonal / 2
                    #alignment script causes images cropped "too closely" to get a bit fucky, so crop them less severely.
                    padding = (bottom - top)/2

                    if((top - padding >= 0) and (bottom + padding <= vidheight) and (left - padding >= 0) and (right + padding <= vidwidth)):
                        croppedframe = rgb_frame[int(top - padding):int(bottom + padding), int(left - padding):int(right + padding)]
                        #if the image is too small, resize it to outputsize
                        cheight, cwidth, cchannels = croppedframe.shape
                        if (cheight < 256) or (cwidth < 256):
                            croppedframe = cv2.resize(croppedframe, outputsize, interpolation=cv2.INTER_CUBIC)
                        print("Worker {} found a result!".format(procId))
                        resultQueue.put( croppedframe )
        except queue.Empty:
            pass
    print("Worker {} done!".format(procId))

###############################
# program entry point
#
if __name__ == "__main__":
    os.system('cls' if os.name=='nt' else 'clear')
    args = parseArgs()
    main( args )